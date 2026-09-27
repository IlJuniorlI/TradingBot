# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field, fields
from copy import deepcopy
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

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
from .event_blackouts import blackout_row_errors, earnings_errors
from .models import PairDefinition, StrategySchedule, Window
from .serialization import read_yaml
from ._strategies.catalogue import (
    default_strategy_name,
    get_plugins,
    is_option_strategy,
    normalize_strategy_name,
    plugin_names,
)
from ._strategies.factory import normalize_strategy_params
from .indicators import set_runtime_indicator_mode, set_session_indicator_window
from .sessions import is_hhmm, parse_hhmm
from .symbols import normalize_symbol_list, ticker_quote_hint

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
    "CandlesConfig",
    "ChartPatternsConfig",
    "DashboardChartConfig",
    "DashboardChartingConfig",
    "DashboardConfig",
    "EquityExecutionConfig",
    "PaperConfig",
    "RiskConfig",
    "RuntimeConfig",
    "SchwabConfig",
    "SharedEntryLogicConfig",
    "SharedExitLogicConfig",
    "StrategyConfig",
    "SupportResistanceConfig",
    "TechnicalLevelsConfig",
    "TradingViewConfig",
    "ZeroDteOptionsConfig",
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
    # Forwards to schwabdev.Client(open_browser_for_auth=...) (added in
    # schwabdev 3.0.4). When True (default), the OAuth flow auto-opens a
    # browser tab so the user can complete authentication. Set False on
    # headless deployments (servers, CI, containers without a browser) —
    # the auth URL will be logged instead so it can be opened manually.
    open_browser_for_auth: bool = True


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
    # Same-level retry block: after any exit, win or loss, block
    # *same-direction* re-entry on the same symbol for
    # same_level_block_minutes minutes while the new entry sits within
    # same_level_block_atr_mult * ATR of the prior ENTRY, the level already
    # tried (an option's direction and entry are its underlying's; see
    # RiskManager.same_level_anchor). Prevents the NVDA-style breakout-chase
    # pattern. For an equity, a fib-pullback entry (see
    # _fib_pullback_override) overrides the block. 0 minutes = off.
    same_level_block_minutes: int = 30
    same_level_block_atr_mult: float = 0.3
    # --- Entry slippage / realized-risk controls (2026-09-18) ---
    # Position size is computed BEFORE the order from the previewed limit
    # price, but realized risk is qty * |fill - stop|. An adverse fill
    # therefore risks more than the budget, and the overage scales inversely
    # with stop distance: on a 1%-wide stop a 5c slip is ~2% over, but on a
    # structural stop 10x tighter the same slip is ~25% over.
    #
    # entry_slippage_allowance_* pads the stop distance used for SIZING by an
    # expected-slippage amount, so a fill that slips by up to that amount
    # still lands inside the budget. Derived from the live spread (the actual
    # cost of crossing) and capped as a fraction of price so one pathological
    # quote cannot size a trade to zero. Set the spread fraction to 0.0 to
    # size on the raw stop distance, as before.
    entry_slippage_allowance_spread_frac: float = 1.0
    entry_slippage_allowance_max_pct: float = 0.002
    # Post-fill reconciliation. Realized risk is compared against the budget
    # and anything beyond this fraction over is logged and stamped on the
    # position. Detection only — the shares are already bought, so there is
    # nothing to reject; the sizing allowance above is the preventive half.
    risk_overage_warn_frac: float = 0.15
    # Entry slippage beyond this fraction of the signal price is logged and
    # flagged on the position, so a routing or liquidity degradation surfaces
    # in the log rather than only in an end-of-day report nobody diffs.
    entry_slippage_warn_pct: float = 0.0015
    # --- Daily loss projection (2026-09-18) ---
    # max_daily_loss gates NEW ENTRIES on REALIZED P&L only, and open
    # positions are never flattened when it trips. With max_positions open at
    # full risk when realized P&L reaches the limit, the day can finish at
    # roughly twice it. When this is on, can_open subtracts the open
    # positions' remaining risk-to-stop from realized P&L before comparing,
    # so the bot stops opening new trades once the WORST CASE would breach
    # the limit rather than once the realized damage already has.
    #
    # Remaining risk per position is measured to its CURRENT stop, so a stop
    # trailed to breakeven or better contributes zero — this tightens the gate
    # without punishing positions that are already de-risked. Set False to
    # restore the realized-only comparison.
    daily_loss_includes_open_risk: bool = True
    # Time-stop: scratch a trade held too long without meaningful price
    # movement. 2026-04-17 META held 223 min for +$0.16 on EQL exit — dead
    # capital blocking a slot. 0 = disabled.
    time_stop_minutes: int = 45
    time_stop_min_return_pct: float = 0.003
    # Peak-giveback floor: once the trade's max_favorable_r (peak R since
    # entry) crosses peak_giveback_min_r, force an exit when current_r
    # retraces to the floor, a tiered fraction of the peak it keeps: the
    # peak_giveback_retain_* fractions below (0.65 of a 1R-2R peak, 0.72 of
    # 2R-3R, 0.78 of 3R+ by default; 0.50 / 0.60 / 0.70 until 2026-05-27).
    # The tiers start at a 1R peak whatever min_r is: below 1R only the low
    # tier (below) has a floor. A position whose entry stamped
    # peak_giveback_min_r_override (the high-conviction day override) arms
    # at that override instead, with no low tier. Complements Fix 4
    # (protective BE at +0.5R): BE catches 0.5-1R winners, this catches 1R+
    # runners that give back too much. 2026-04-17 this would have locked
    # INTC +$95 → +$47 instead of -$3, AMZN +$62 → +$31 instead of -$13, AMD
    # +$31 → +$15 instead of -$30. Net modeled improvement ~+$135 on the
    # session (alongside BE fix). peak_giveback_enabled: false turns the
    # giveback off (the low tier with it); peak_giveback_min_r must be above
    # 0 (a 0 read as 1.0 until 2026-09-26, and is refused at load now).
    peak_giveback_enabled: bool = True
    peak_giveback_min_r: float = 1.0
    # Low-tier peak-giveback (2026-05-26). The main peak-giveback gate only
    # arms when peak_r ≥ peak_giveback_min_r (default 1.0), which means
    # trades that peak in the 0.5-1.0R range and retrace through entry
    # round-trip back to the BE / fixed stop with no exit floor. Session
    # 2026-05-14 had 5 such trades (TSLA 0.70R, AMD 0.74R, GOOG 0.97R,
    # META 0.54R, AAPL 0.60R) all stopped at ~BE with cumulative -$50;
    # 5/26 added INTC#4 (0.80R, -$3) and NEM (0.58R, -$10). The low tier
    # catches these by arming when peak_r reaches peak_giveback_low_tier_min_r
    # (default 0.7R) and exiting when current_r retraces past
    # peak * (1 - peak_giveback_low_tier_giveback_frac). Defaults are
    # conservative: 70% giveback (floor at 30% of peak) so a 0.7R peak
    # arms at floor 0.21R — enough to wiggle but not enough to lose
    # the trade entirely. Skipped when the high-conviction override is
    # active (those trades want the wider main-tier leash). Set
    # peak_giveback_low_tier_enabled=False to disable entirely.
    peak_giveback_low_tier_enabled: bool = True
    peak_giveback_low_tier_min_r: float = 0.7
    peak_giveback_low_tier_giveback_frac: float = 0.7
    # Main-tier peak-giveback retain fractions (2026-05-27): fraction of the
    # peak R kept before the give-back floor fires, by peak-size tier.
    # Tightened from the original 0.50/0.60/0.70 after the 5/12-5/27 sample
    # showed winners captured only 44% of their MFE (gave back 56% of peak
    # gains) — and made no new highs after their interim peak in-sample, so
    # the loose floors donated realized gains. Modeled +3.7R additional
    # capture across 8 winners (~$465 at full size). Higher = capture more /
    # exit sooner on a retrace; lower = more recovery room (risks clipping
    # a retrace-then-recover between the old and new floor). At a 2R peak,
    # 0.65 exits on a retrace to 1.3R (was 1.0R at 0.50).
    peak_giveback_retain_1to2r: float = 0.65
    peak_giveback_retain_2to3r: float = 0.72
    peak_giveback_retain_3r_plus: float = 0.78


@dataclass(slots=True)
class RuntimeConfig:
    loop_sleep_seconds: float = 2.0
    # --- Persistent-failure escalation (2026-09-18) ---
    # The main loop already backs off exponentially on errors and never gives
    # up, but nothing said so out loud: a sustained outage during the
    # management window left open positions unmanaged with only a throttled
    # WARNING line to show for it. After this many consecutive failed cycles
    # the engine escalates to a CRITICAL log naming the open positions, and
    # the dashboard status turns into an explicit alarm. At the capped 60s
    # backoff, 10 cycles is roughly 10 minutes of no management.
    # Set to 0 to disable the escalation (the backoff is unaffected).
    error_escalation_cycles: int = 10
    # When the bot is in "deep idle" (always-on mode with
    # auto_exit_after_session=false, outside the 7am–8pm ET equity stream
    # session, and no open positions), the main loop sleeps this long
    # between cycles instead of `loop_sleep_seconds`. Cuts overnight CPU
    # waste by ~95% — the loop wakes up every minute to recheck whether
    # the stream session has started, instead of every 2s. Set to a
    # value <= loop_sleep_seconds (0 included; it read as 60 until
    # 2026-09-26) to disable the optimization entirely (no idle slowdown).
    idle_sleep_seconds: float = 60.0
    # How often the engine evicts per-symbol state (history frames, HTF
    # caches, dashboard snapshot/chart payloads) for symbols that have
    # dropped out of the active set (streaming + watchlist + open
    # positions). Long-running multi-day runs accumulate per-symbol
    # entries — each 1m frame is ~240KB at the default lookback, so 500
    # symbols seen over a month would be ~120MB just for history.
    # Pruning runs every cycle on a wall-clock timer so the per-cycle
    # cost is negligible. Set to 0 to disable pruning entirely.
    symbol_state_prune_seconds: float = 1800.0
    history_poll_seconds: int = 300
    quote_poll_seconds: int = 6
    quote_cache_seconds: int = 6
    quote_batch_size: int = 20
    history_lookback_minutes: int = 390
    use_extended_hours_history: bool = True
    use_rth_session_indicators: bool = True
    # Which session window the session indicators key off (only when
    # use_rth_session_indicators is true). VWAP/EMA reset at each session's
    # open; the TA-Lib columns (ATR/RSI/ADX/OBV...) do not reset: on session
    # bars they are one session-only series stitched across sessions
    # (gap-neutral), and bars outside the window keep the all-hours series.
    # "rth" = the 09:30-16:00 regular session (default). "extended" = the
    # 07:00-20:00 equity stream window, for strategies that enter pre/post
    # market (top_tier_adaptive extended-hours mode). Leave "rth" for every
    # RTH-only strategy.
    equity_session_indicator_window: str = "rth"
    warmup_minutes: int = 90
    prewarm_before_windows_minutes: int = 5
    log_dir: str = ".logs"
    stream_fields: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6, 7, 8])
    stream_connect_timeout_seconds: int = 20
    stream_fallback_poll_seconds: int = 25
    stream_stale_fallback_seconds: int = 180
    stream_health_log_seconds: int = 90
    reconcile_on_startup: bool = True
    startup_reconcile_mode: str = "block"
    startup_order_lookback_days: int = 2
    startup_reconcile_ignore_symbols: list[str] = field(default_factory=list)
    startup_reconcile_metadata_db_path: str = ".logs/startup_reconcile_metadata.sqlite"
    # When the bot stays running across ET midnight (always-on operation
    # with `auto_exit_after_session: false`), re-run the startup reconcile
    # at the first cycle on each new trading day where streaming is back
    # online (i.e. the first cycle after 7am ET). Catches positions that
    # closed overnight via the Schwab app or broker-side stops — without
    # this, the bot would wake at 7am still believing those positions
    # are open and try to manage phantoms. Honors the same
    # `reconcile_on_startup` and `startup_reconcile_mode` knobs as the
    # startup reconcile (no separate mode). Set to `false` to disable
    # if you handle reconciliation externally or only run single-day
    # sessions.
    session_reconcile_on_resume: bool = True
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
    # When True, the engine writes a per-day archive to
    # {log_dir}/sessions/{YYYY-MM-DD}/ (once per ET trading day after 20:00,
    # and again on shutdown) containing bars/1m/{SYMBOL}.csv (the full merged
    # 1m frame with indicators, extended hours and warmup included),
    # bars/{N}m/ resamples for ltf/htf minutes above 1, bars/htf_{N}m/ (the
    # stored HTF frame levels are built from), trades.csv filtered to the
    # day, decisions.csv and manifest.json with strategy + summary stats; see
    # session_report.export_session_archive for the full list. Disable to
    # save disk space if running without dashboard/analysis needs.
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
    show_fib_retracements: bool = False
    show_channel: bool = False
    show_trendlines: bool = False
    show_htf_fair_value_gaps: bool = False
    show_ltf_fair_value_gaps: bool = False
    # Order blocks render as dashed-stroke rectangles (vs FVGs' solid fill)
    # so the two zone types are visually distinguishable on the chart.
    show_htf_order_blocks: bool = False
    show_ltf_order_blocks: bool = False
    # Divergence trendlines on the price chart. RSI divergences render as
    # solid (regular) or dashed (hidden) lines connecting the two pivot
    # points. OBV is off by default to avoid stacking duplicate lines on
    # nearby pivot pairs — toggle on if you want both indicators visible.
    show_rsi_divergence: bool = True
    show_obv_divergence: bool = False
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
            show_fib_retracements=True,
        )
    )

    def resolved_profile(self, mode: str) -> DashboardChartConfig:
        """The ``expanded`` profile for that mode, else ``compact``.
        ``load_config`` checks each profile's ``max_bars`` (an integer in
        1-480) and switches; until 2026-09-26 this read an unreadable
        ``max_bars`` as the profile's default, clamped it to 1-480 and read
        any string as a switch that is on."""
        return self.expanded if str(mode or "").lower() == "expanded" else self.compact


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

    # --- Broker-side bracket (first-triggers-OCO) orders ---
    # When enabled the entry is submitted as a single Schwab TRIGGER order
    # whose child OCO carries the protective stop (and optionally the target),
    # so the exit rests AT THE BROKER instead of waiting for the engine's
    # management poll. Off by default: every existing preset keeps today's
    # fully engine-managed exits.
    bracket_orders_enabled: bool = False
    # static  = submit once, never touch. Broker owns the resting levels for
    #           the life of the trade. Correct for fixed-stop/fixed-target
    #           scalps that do no in-trade level management.
    # replace = engine keeps managing levels; every stop/target move issues a
    #           replace_order against the corresponding child. Required for the
    #           adaptive/ladder strategies whose edge is the in-trade ratchet.
    bracket_sync_mode: str = "static"
    # stop_and_target = both OCO children rest at the broker.
    # stop_only       = only the protective stop rests; the target stays
    #   engine-side. REQUIRED for `trade_management_mode: adaptive_ladder`
    #   with `shared_exit.adaptive_ladder_touch_hold` on: the hold declines
    #   the target exit at a touched rung until that bar's close decides it,
    #   then moves the target to the next rung or clears it past the last
    #   one (a runner). A resting target limit fills through all of that.
    #   With the hold off the ladder's first rung is a plain take-profit,
    #   which a resting target serves as well.
    bracket_legs: str = "stop_and_target"
    # STOP fills wherever a flush ends — punishing on thin small caps.
    # STOP_LIMIT bounds the slippage at the cost of a no-fill tail risk.
    bracket_stop_order_type: str = "STOP_LIMIT"
    # STOP_LIMIT only: limit offset below (LONG) / above (SHORT) the stop
    # trigger, expressed in units of initial R.
    bracket_stop_limit_offset_r: float = 0.5
    # Schwab rejects STOP orders outside the NORMAL session. With this true a
    # bracketed entry is REJECTED pre/post market rather than silently sent
    # naked; set false only if you accept unbracketed extended-hours entries.
    bracket_require_normal_session: bool = True
    # replace mode debounce: skip the replace_order round trip unless a level
    # moved at least this many dollars. Stops the per-cycle trail ratchet from
    # burning the Schwab rate budget on sub-penny adjustments.
    bracket_replace_min_price_delta: float = 0.01


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
    ltf_fair_value_gaps_enabled: bool = True
    # Shared FVG tuning knobs — both LTF and HTF FVG detection read these.
    # Each timeframe has its own enable flag above; the size filters and
    # max-per-side cap are identical across timeframes (matches the OB
    # convention). LTF detection runs on the strategy's `params.ltf_minutes`
    # frame (defaults to 1-minute streaming bars when not declared).
    fair_value_gap_max_per_side: int = 3
    fair_value_gap_min_atr_mult: float = 0.06
    fair_value_gap_min_pct: float = 0.0006
    # Order block detection. Two timeframes: LTF (the strategy's
    # `params.ltf_minutes` frame, default 1-minute streaming bars) and HTF
    # (controlled by `support_resistance.timeframe_minutes`, default 15m).
    # Each timeframe has its own enable flag; the six tuning knobs below
    # are SHARED — both timeframes use the same mode / max_per_side / size
    # filters / pivot span.
    # Disabled by default. When `ltf_order_blocks_enabled` is true,
    # strategies that call `_continuation_ob_retest_plan` get a parallel
    # retest gate that ORs with the FVG retest plan. When
    # `htf_order_blocks_enabled` is true, HTF OBs are
    # detected and exposed via `_htf_order_block_context` for strategies that
    # consume them (current strategies don't gate entries on HTF OBs; this
    # mirrors the FVG architecture where HTF FVGs are detected for context
    # but only LTF FVGs gate retest entries).
    # Mode "loose" finds the last opposite-color candle before any close
    # that prints a new local high/low; "strict" requires a formal
    # break-of-structure event using pivot_span swing detection.
    ltf_order_blocks_enabled: bool = False
    htf_order_blocks_enabled: bool = False
    order_block_mode: str = "loose"
    order_block_max_per_side: int = 4
    order_block_min_atr_mult: float = 0.05
    order_block_min_pct: float = 0.0005
    order_block_pivot_span: int = 2
    order_block_new_high_lookback: int = 8
    # Minimum BoS displacement (in ATR units) required for an OB to count.
    # The break-of-structure bar's close must be at least this much away
    # from the OB candle's close. Filters out micro-breakouts that would
    # otherwise produce a flood of noise OBs near current price. Combined
    # with the strength-based sort in build_order_block_context, this
    # ensures strong OBs from real moves outrank fresh weak OBs in the
    # top-K cap. 0.0 disables the thrust filter.
    order_block_min_thrust_atr_mult: float = 0.75
    trading_flip_confirmation_1m_bars: int = 2
    trading_flip_confirmation_5m_bars: int = 1
    flip_stop_buffer_atr_mult: float = 0.25
    flip_target_requires_momentum_confirm: bool = True
    regime_weight: float = 0.70
    structure_enabled: bool = True
    structure_ltf_pivot_span: int = 2
    structure_eq_atr_mult: float = 0.25
    structure_ltf_weight: float = 0.65
    structure_htf_weight: float = 0.85
    structure_event_lookback_bars: int = 6
    # BOS/CHoCH freshness window for the HTF structure (the S/R context's
    # `market_structure`, built on `timeframe_minutes` bars), in HTF bars.
    # None = same as `structure_event_lookback_bars`, which keeps every preset
    # that never distinguished the two exactly as it was.
    #
    # It exists because one knob was sizing two different clocks. The LTF
    # value is routinely retuned for the LTF frame -- top_tier halved it 8 -> 4
    # on 2026-05-27 "(= 20 min)" on 5m bars -- and that silently halved the
    # 15m HTF window too, 120 -> 60 minutes, under the HTF bias entry gate.
    # Resolve through `htf_structure_event_lookback`, never read it raw.
    htf_structure_event_lookback_bars: int | None = None
    # Minimum spread between the most recent reference_high and reference_low
    # (in ATR units) for structure-derived bias to be considered meaningful.
    # When EQH and EQL coexist within a tight range (e.g. 0.3 ATR), the bias
    # signals derived from the midpoint check or pivot labels are noise — a
    # single bar can flip bias bearish→bullish multiple times in a narrow
    # consolidation. AMD 14:36 LONG pullback was killed at 10.2m via
    # structure_bearish_exit:EQL where the structure was tight chop. With
    # this gate, ``bias`` defaults to ``neutral`` when range < threshold,
    # suppressing the noisy bias-flip exits. ``eqh`` / ``eql`` flags remain
    # set so range-regime entries (which use the EQ labels) still see them.
    # Set 0.0 to disable. CHoCH still fires from the BoS-event path.
    structure_min_range_atr_mult: float = 1.5
    # Minimum bar separation between consecutive (alternating) structure
    # pivots (2026-05-27, "Fix B"). The pivot detector uses a fractal swing
    # and merges consecutive same-kind pivots, but had NO rule against an
    # alternating H->L registering 1-2 bars apart — so on volatile names a
    # new "swing" prints every few bars and the HH/LH/EQH/LL/HL/EQL labels
    # (and the BOS/CHoCH/structure-exit signals keyed on them) churn on
    # 1-2-bar wiggles. When > 0, an alternating pivot closer than this many
    # bars to the prior kept pivot is treated as noise within the current
    # leg and skipped (the leg continues). Measured on NVDA: a 3-bar gap
    # cut ≤2-bar-apart pivot pairs from 28/91 to 7/59. 0 = disabled
    # (preserves the original behavior for every strategy that doesn't set
    # it). The swings themselves are ATR-sized (median 1.9-5.2 ATR), so
    # this is purely a temporal-density filter, not an amplitude one.
    structure_min_pivot_gap_bars: int = 0
    # Resample the LTF structure frame to this many minutes before pivot
    # analysis (2026-05-27, "Fix D"). The "ltf" structure context runs on
    # the raw streaming frame (1-minute bars), which is finer than the
    # regime-scoring LTF (params.ltf_minutes, typically 5m) — a mismatch
    # that makes structure pivots ~5x denser than the bars the strategy
    # actually trades on. When > 0, _structure_context resamples the LTF
    # frame to this timeframe first, so entry/exit/HTF-alignment all read
    # the coarser structure. 0 = disabled (use the frame as-is). NOTE:
    # raising this rescales every bar-based structure setting —
    # structure_event_lookback_bars, the post-entry pivot count, etc. are
    # now in units of this timeframe. Set structure_event_lookback_bars
    # proportionally lower when enabling (e.g. 6 1m-bars -> 3 5m-bars to
    # keep BOS/CHoCH event freshness near the original ~15-30 min).
    structure_ltf_timeframe_minutes: int = 0
    # Grace window post-entry during which the bias-based structure exits
    # (structure_bearish_exit:EQL/LL/HL, structure_bullish_exit:HH/LH) are
    # suppressed. An EQL pivot forming in the first few minutes after entry
    # is noise, not reversal — session 2026-04-17 had 1W/12T on structure
    # exits, net -$354. The CHoCH exit and the S/R-break exit are not
    # graced: the CHoCH exit only needs a CHoCH that happened after entry
    # (see shared_exit.EXIT_FAMILY_GATES; the 2026-09-24 study found no
    # guard, this grace included, with a measurable effect on it).
    structure_exit_grace_minutes: int = 10
    # Minimum new LTF-structure pivots formed AFTER entry before the bias-
    # based structure exits can fire: pivots whose bar closed after the
    # entry, counted from MarketStructureContext.pivot_times (2026-09-24;
    # it used to compare a rolling-window pivot count against one stamped
    # at entry, which most strategies never stamped). Complements the
    # time-grace by requiring at least some actual structure to form. The
    # CHoCH exit is exempt.
    structure_exit_min_post_entry_pivots: int = 2
    # Extended grace window for PULLBACK entries specifically. Pullback
    # entries are designed to enter into LTF chop (buy the dip on a bullish
    # HTF) — the first EQL/LL pivot 10 min in is almost always noise, not
    # a reversal. Session 2026-05-14 AMD pullback was killed at 10.2m via
    # structure_bearish_exit:EQL; price recovered above the target shortly
    # after. Applies when position.metadata.entry_style_family == "pullback"
    # (stamped at entry; it keyed on top_tier's regime == "pullback" until
    # 2026-09-24), as max(this, structure_exit_grace_minutes), so a value
    # <= structure_exit_grace_minutes disables the override.
    structure_exit_grace_minutes_pullback: int = 15
    # When True, structure_bearish_exit / structure_bullish_exit (the bias-
    # based bias-flip exits, NOT CHoCH) additionally require an active
    # BoS event in the matching direction (bos_down for long-exit, bos_up
    # for short-exit) that happened after entry. Without this gate, bias
    # flips on a single EQL/HH pivot — a noisy, weak signal that aborts
    # otherwise-healthy pullback trades. With BoS confirmation we require
    # price to have actually broken below a prior swing low (or above for
    # short exits). The CHoCH exit is unaffected.
    structure_exit_require_bos_confirmation: bool = True
    # Extended grace window for opening-range entries
    # (position.metadata.entry_style_family == "orb": top_tier's ORB regime,
    # opening_range_breakout, microcap_gap_orb; until 2026-09-24 it keyed on
    # orb_window_entry, which only top_tier stamped). ORB pullbacks often
    # look like bearish structure breaks / bearish chart patterns but
    # continue higher afterward. 2026-04-24: 5 of 6 ORB entries lost via
    # pullback-driven exits (INTC at 2.0m, AMD at 11.2m via
    # structure_bearish_exit:HL, etc). When set > 0, suppresses both
    # structure_bearish/bullish_exit AND chart_pattern_exit for the first N
    # minutes of the trade. The CHoCH exit is not graced (see
    # structure_exit_grace_minutes). Set 0 to disable.
    orb_entry_exit_grace_minutes: int = 20

    def flip_confirmation_bars(self) -> tuple[int, int]:
        """``(bars_1m, bars_5m)`` for the dual-frame level-flip confirmation.

        Either frame confirming a flip is enough; 0 switches that frame's gate
        off, so ``(0, 1)`` confirms on 5m bars only and ``(2, 0)`` on 1m only.
        Until 2026-09-23 every reader spelled this ``int(value or 2)`` /
        ``int(value or 1)``, which turned a configured 0 back into the default
        and made the single-frame modes impossible to configure. The one place
        these two knobs are read; ``load_config`` rejects negatives.
        """
        return int(self.trading_flip_confirmation_1m_bars), int(self.trading_flip_confirmation_5m_bars)

    def htf_structure_event_lookback(self) -> int:
        """HTF BOS/CHoCH freshness window in HTF bars: the explicit
        ``htf_structure_event_lookback_bars`` when set, else the shared
        ``structure_event_lookback_bars``. The one place that fallback lives."""
        if self.htf_structure_event_lookback_bars is not None:
            return max(1, int(self.htf_structure_event_lookback_bars))
        return max(1, int(self.structure_event_lookback_bars or 6))


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
    # ATR multiple a close must clear past a trendline to count as a break.
    # 0.65 (was 0.15, 2026-09-23): on large caps the old value never applied,
    # because a 0.10%-of-price floor always won; 0.65 reproduces that floor
    # at the median on 1m bars. The ATR is atr_with_floor = max(ATR14, 0.15% of
    # price), so the buffer scales with volatility only above that floor,
    # which decides ~70-80% of 1m large-cap bars. The 5m presets use 0.30.
    # The small-cap presets (small_cap_squeeze, microcap_*, and the $2-$20
    # screener presets momentum_close, mean_reversion, closing_reversal,
    # opening_range_breakout) keep 0.15, which on their tape was already the
    # buffer.
    trendline_breakout_buffer_atr_mult: float = 0.65
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
    # Counter-direction LTF divergence penalties on the entry score. They are
    # terms of shared_entry.use_technical_entry_adjustment (with the hidden
    # bonuses below), not of any divergence knob. The dual RSI+OBV counter
    # divergence VETO is shared_entry.use_dual_divergence_veto; its old second
    # switch here, divergence_block_dual_counter, was removed 2026-09-24.
    divergence_counter_rsi_penalty: float = 0.12
    divergence_counter_obv_penalty: float = 0.10
    # Multi-pivot detection (divergence.find_divergence): walk the last N
    # pivots and return the most recent qualifying pair, gated by max-age in
    # bars so stale divergences don't dominate.
    # divergence_pivot_lookback, divergence_min_price_move_pct and
    # divergence_rsi_min_delta are shared by the LTF and the HTF divergence:
    # the data feed passes them to every HTF build, in its cache key, since
    # 2026-09-25 (until then the HTF used the builder's defaults, which every
    # preset matches, so no build changed). divergence_rsi_length is
    # LTF-only; the HTF reads its frame's rsi14.
    divergence_pivot_lookback: int = 4
    divergence_max_age_bars: int = 8
    # The same limit for the HTF RSI divergence (HTFContext.*_rsi_divergence,
    # scored by shared_entry.use_htf_divergence_score and drawn by the
    # dashboard), in
    # HTF bars. Both ages count session bars only while session indicators
    # are on and the clock is inside the session, so yesterday's last pivots
    # stay live across the overnight. The data feed reads this, enabled,
    # divergence_enabled and the three shared thresholds named above once
    # in its HTF context build, for every strategy and the dashboard alike
    # (they are part of its cache key); until 2026-09-24 every HTF build
    # used 6 with divergence always on. 6 is that value, not a retune.
    htf_divergence_max_age_bars: int = 6
    divergence_min_price_move_pct: float = 0.0015
    # Hidden divergence (continuation pattern) — same-direction bonus on the
    # entry score. Bullish hidden div on a LONG entry: bonus. Bearish hidden
    # div on a SHORT entry: bonus. Counter-direction hidden div has no effect
    # (regular divergence already grades that case).
    divergence_hidden_bonus_rsi: float = 0.10
    divergence_hidden_bonus_obv: float = 0.08
    # HTF (multi-timeframe) divergence confluence — read from
    # HTFContext.{bullish,bearish}_rsi_divergence by the shared entry policy's
    # HTF divergence score term (shared_entry.use_htf_divergence_score).
    # _rsi suffix because HTF divergence is RSI-only (OBV is volume-driven
    # and HTF resampling smears the signal — htf_levels.build_htf_context
    # intentionally skips HTF OBV computation).
    # Aligned: HTF div in same direction as the trade. Counter: HTF div
    # against the trade. Hidden: same-side hidden div (continuation).
    htf_divergence_aligned_bonus_rsi: float = 0.20
    htf_divergence_counter_penalty_rsi: float = 0.25
    htf_divergence_hidden_bonus_rsi: float = 0.10
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
    """The shared entry knobs. Global since 2026-09-24: every strategy hands
    each candidate entry to ``_strategies/shared_entry.SharedEntryPolicy``
    (``admit`` / ``emit``), the ONLY reader of this section, so a knob acts on
    every strategy whose YAML sets it -- no strategy calls a knob helper or
    can rewrite a knob. Until then each strategy called the helpers it chose
    to, so most knobs silently did nothing for most strategies (key_levels
    read three of them), and a ``strategy_logic_default`` hook let a strategy
    rewrite any of them. A strategy exempts one of its styles from a veto
    only by declaring it in its manifest
    (``capabilities.shared_entry.exemptions``).

    The policy's order per proposal: the raw R:R gate a builder asked for;
    the FVG / order-block retest admission of deferrable reasons; the vetoes
    (structure, S/R, broken level, chart, dual divergence, candle -- all of
    them evaluated, so every blocker is logged); the S/R then technical
    stop/target refinement; the score terms.
    """

    # FVG context: the retest admission of deferrable entry reasons (the
    # anti-chase FVG plan), the fvg_entry_adjustment score term and the
    # continuation / reversal bias the strategies feed their runner and
    # management; zero_dte reads its regime FVG scores through the policy.
    use_fvg_context: bool = True
    # VETO an entry when RSI AND OBV both diverge against it on the entry
    # frame. Renamed from use_divergence_filter (2026-09-24): the old name
    # and the docs claimed it also drove the LTF counter-divergence
    # penalties, which belong to use_technical_entry_adjustment. Its old
    # second switch, technical_levels.divergence_block_dual_counter, is gone.
    use_dual_divergence_veto: bool = True
    # The HTF RSI divergence SCORE term: +htf_divergence_aligned_bonus_rsi
    # for an aligned HTF divergence, -htf_divergence_counter_penalty_rsi for a
    # counter one, +htf_divergence_hidden_bonus_rsi for a same-side hidden one
    # (technical_levels). It never blocks. Renamed from
    # use_htf_divergence_filter (2026-09-24), which it never was. The HTF
    # divergences are None whenever technical_levels.enabled or
    # divergence_enabled is off, so the term needs no other switch.
    use_htf_divergence_score: bool = True
    # Divergence as an entry trigger of its own (off in every preset). With
    # it on, a candidate the strategy produced no signal for may enter on a
    # confirmed LTF RSI/OBV divergence (regular = reversal, hidden =
    # continuation, the hidden one only with the HTF EMAs aligned) at S/R
    # confluence whose latest pivot is in the current session. It passes
    # through every veto, the refinement and min_shared_context_score like
    # any entry, and always ranks behind the strategy's own signals. When
    # the strategy's own proposal and a divergence agree on direction, the
    # proposal's shared score gains divergence_entry_score_bump; when they
    # disagree the proposal wins and is only stamped with the conflict. The
    # stop sits support_resistance.stop_buffer_atr_mult ATR beyond the
    # divergence pivot; the target is min_target_rr * risk, and an opposing
    # S/R level inside that distance refuses the candidate (with
    # min_target_rr off the target is the opposing level, or none: a
    # runner). A symbol the strategy skipped before evaluating a setup (not
    # its symbol, outside its window, too few bars) is never entered on a
    # divergence. A strategy opts out in its manifest
    # (capabilities.shared_entry.divergence_entry: false: pairs_residual,
    # the two zero_dte strategies).
    use_divergence_entry_signal: bool = False
    divergence_entry_min_age_bars: int = 0
    divergence_entry_require_sr_confluence: bool = True
    divergence_entry_score_floor: float = 1.5
    divergence_entry_score_bump: float = 0.20
    # The technical score term: channel / trendline / Bollinger / ADX /
    # anchored-VWAP / ATR / OBV terms and the LTF divergence penalties and
    # hidden-divergence bonuses (technical_levels.divergence_*).
    use_technical_entry_adjustment: bool = True
    use_technical_stop_target_refinement: bool = True
    # VETO an entry against the LTF market structure (a fresh opposing CHoCH,
    # or an opposing bias without a fresh same-side BoS), on the frame the
    # strategy declares as its gate frame.
    use_structure_filter: bool = True
    # VETO an entry pressed against the HTF S/R level ahead of it (inside
    # support_resistance.entry_min_clearance_pct OR _atr) or through a broken
    # level with none on its own side.
    use_sr_filter: bool = True
    use_sr_stop_target_refinement: bool = True
    # VETO an entry within broken_level_min_clearance_pct (scaled by the
    # proposal's volatility scale) or broken_level_min_clearance_atr of a
    # confirmed BROKEN level beyond it (broken_support under a LONG,
    # broken_resistance over a SHORT). Moved out of top_tier_adaptive
    # (reject_entry_near_broken_level, 2026-09-24), where it existed to keep
    # an entry clear of the S/R-loss exit's trigger; that exit ships off, and
    # the setups the guard alone blocked did no worse than real fills, so it
    # ships off in every preset too. The thresholds are top_tier's.
    use_broken_level_guard: bool = False
    broken_level_min_clearance_pct: float = 0.0025
    broken_level_min_clearance_atr: float = 0.72
    # VETO an entry against an opposing chart pattern. Runs after the retest
    # admission (2026-09-24): it used to run only when no other reason was
    # pending, so an entry the FVG retest admitted never met it.
    use_opposing_chart_filter: bool = True
    # VETO an entry when the opposing-direction candle cluster reaches
    # candles.opposing_net_score_threshold. Uses the cached candle context
    # (no extra ta-lib calls). Until 2026-09-24 only top_tier read it.
    use_opposing_candle_filter: bool = False
    # Minimum risk-to-reward floor enforced by the SR/technical-level
    # target refinement pipeline. When a refine pass would cap the target
    # so close to entry that R:R drops below this value, the cap is
    # rejected and the strategy's original target is kept. Protects
    # against the "$0.10 target" bug where nearby S/R levels collapse R:R
    # toward zero. Default 1.0 = require at least 1:1 R:R after any
    # refinement. 0 or null switches the floor off (a positive reward is
    # still required); until 2026-09-24 a configured 0 read as 1.0. Also
    # the raw R:R gate a builder can ask for (top_tier's orb and sr_scalp)
    # and the divergence entry's target floor.
    min_target_rr: float | None = 1.0
    # Floor on how far the SR/technical refinement pipeline may pull a stop
    # TOWARD entry, in ATR14 units. min_target_rr cannot police this: pulling
    # the stop in RAISES reward/risk, so the R:R guard never binds on the
    # stop side. Without an absolute floor, a support level sitting a few
    # cents under entry produced a few-cent stop and silently overrode the
    # `default_stop_pct` backstop each strategy builder applies — measured
    # over 2026-05-12..29, 46 of 57 top_tier_adaptive entries had their stop
    # pinned exactly to `nearest_support - level_buffer`, a median 2x (worst
    # 10x) tighter than the builder floor. The floor only refuses to TIGHTEN;
    # a builder that deliberately set a stop inside this distance keeps it.
    # Since 2026-09-24 it also bounds the FVG / order-block retest stop
    # anchor, which used to pull a stop to 0.05% from entry past it. 0 or
    # null switches the floor off.
    min_stop_atr_mult: float | None = 1.5
    # Optional floor on the proposal's shared score (entry_context_adjustment
    # + fvg_entry_adjustment + the divergence bump): an entry below it is
    # refused as shared_context_below_min. null = no floor (as shipped).
    min_shared_context_score: float | None = None


@dataclass(slots=True)
class SharedExitLogicConfig:
    use_technical_exit: bool = True
    use_trendline_break: bool = True
    use_channel_break: bool = True
    use_bollinger_reject: bool = False
    use_anchored_vwap_loss: bool = True
    # Require TWO consecutive bar closes through the anchored-VWAP floor/
    # ceiling before the anchored_vwap_loss / _reclaim exit fires (2026-05-29).
    # A single bar's dip below the floor is normal continuation noise in a
    # trending stock; the 2-bar confirm avoids exiting a working trade on one
    # noise bar. Set false to fire on the first qualifying bar (original).
    anchored_vwap_exit_require_two_bar_confirm: bool = True
    use_chart_pattern_exit: bool = False
    # Fire candle_pattern_exit when an opposing-direction candle cluster
    # crosses candles.opposing_net_score_threshold and the tape confirms
    # (confirm_with_ema9/ema20/vwap/close_position below). Reuses the
    # cached candle context — no extra ta-lib calls.
    use_candle_pattern_exit: bool = False
    use_structure_exit: bool = True
    # Exit on a confirmed break (broken_support / broken_resistance) of a
    # level on the ADVERSE side of entry, with the tape confirming. Until
    # 2026-09-24 it sat behind discretionary_exit_min_r, which it can never
    # pass (price is through a level beyond entry, so R < 0), so it was dead
    # in every preset; it is exempt now and reachable. It ships OFF, in
    # every preset and as the code default: a full replay (26 sessions, 235
    # positions) fired it on 13 positions, all top_tier_adaptive, at a
    # median -0.72R, and against today's management (time stop, peak
    # giveback) it cost -0.43R per firing, CI [-0.85, -0.05]. Every variant
    # tried (a 5-20 min grace, a 0.25-0.75R loss cap, no tape confirmation)
    # was negative too. Turning it on is in effect a tighter stop at about
    # -0.7R.
    use_sr_loss_exit: bool = False
    # Minimum open profit (in initial-risk R units) before the discretionary
    # exit families may fire: the bias-based structure exits and the
    # technical exits (trendline / channel / bollinger / anchored-VWAP).
    # Below this threshold the protective stop governs the trade. These
    # families carry no R condition of their own — only grace windows and
    # tape confirmation — so they were cutting trades that had barely
    # moved: over 2026-05-12..29 they closed 19 of 48 top_tier_adaptive
    # trades at a median MFE of 0.09-0.29R for -$831 combined, and in the
    # widest-stop bucket 0 of 22 trades ever reached their stop because a
    # discretionary exit got there first. The CHoCH exit is exempt, as it
    # always was (an ungated structural stop-tightener, typically below about
    # -0.4R). The S/R-break exit is exempt since 2026-09-24: for an equity it
    # fires only on the losing side of entry, so the gate had kept it dead;
    # an option's R is on its premium mark (see
    # shared_exit.EXIT_FAMILY_GATES). Set to 0 to restore the un-gated
    # behaviour.
    discretionary_exit_min_r: float = 0.5
    # The adaptive ladder's touch hold (risk.trade_management_mode:
    # adaptive_ladder, 2026-09-27). OFF in every preset and as the code
    # default: the ladder's first rung is then a plain take-profit, taken on
    # the first quote at it. (It replaces a target-exit suppression and a
    # zone-flip rung promotion, removed: after 2026-05-14 the suppression
    # acted only when the quote sampling missed a strong 1m close through
    # the target - in the replays only with a quote once a minute, in three
    # trades, two better and one worse for this default - and such a touch
    # now takes the target.)
    # On: the first quote at the target holds the position -- no target exit
    # -- until the 1m bar the quote was fetched in is delivered, then judges
    # that bar once. A close at or through the target at least 55% of the
    # way up its range (down, for a SHORT) promotes the rung: the stop to the
    # rung less the ladder's stop buffer (the S/R level buffer, a quarter of
    # the rung's zone width or 0.05% of the price, whichever is widest), the
    # target to the next rung that close has not passed, or none past the
    # last rung (a runner). Any other close exits at market
    # (target_weak_close). While it holds, a quote the stop buffer back
    # through the rung exits at once (target_hold_guard), and a touch bar
    # still undelivered this many seconds after it closed exits at market
    # (target_hold_timeout). There is no index veto. Each touch and verdict
    # is logged on one INFO line (LADDER_TOUCH / LADDER_VERDICT) for a
    # dry-run A/B. Needs execution.bracket_legs: stop_only with brackets.
    # Both are checked at load (_NUMBER_CHECKS["shared_exit"], the switch
    # check).
    adaptive_ladder_touch_hold: bool = False
    adaptive_ladder_touch_hold_timeout_seconds: float = 45.0
    # Divergence exit (counter-direction REGULAR divergence forms while
    # holding). LONG + new bearish RSI/OBV div -> consider partial close.
    # SHORT + new bullish div -> mirror. Hidden divergence is continuation
    # context and does NOT trigger this exit.
    use_divergence_exit_signal: bool = False
    divergence_exit_partial_frac: float = 0.5
    divergence_exit_min_age_bars: int = 1
    divergence_exit_require_in_profit: bool = True
    confirm_with_ema9: bool = True
    confirm_with_ema20: bool = True
    confirm_with_vwap: bool = True
    confirm_with_close_position: bool = True
    bullish_close_position_max: float = 0.46
    bearish_close_position_min: float = 0.54
    bullish_close_position_loose_max: float = 0.38
    bearish_close_position_loose_min: float = 0.62


@dataclass(slots=True)
class EventsConfig:
    """Scheduled-event blackouts shared by every strategy.

    Lifted out of ``ZeroDteOptionsConfig`` (2026-09-18) — the blackout
    calendar was reachable only from the 0DTE options strategy, so equity
    strategies had no macro-event awareness and no earnings awareness at all.
    See ``event_blackouts.EventBlackoutCalendar`` for the evaluation.
    """

    enabled: bool = True
    # Macro windows (CPI / FOMC / ...). Same schema the 0DTE path used, plus
    # an optional ``symbols`` list to scope a window to specific tickers.
    blackout_file: str | None = "./macro_events.auto.yaml"
    blackouts: list[dict[str, Any]] = field(default_factory=list)
    # Per-symbol earnings dates: {"AAPL": ["2026-10-30", ...], ...}
    earnings_file: str | None = "./earnings.yaml"
    earnings: dict[str, list[str]] = field(default_factory=dict)
    # Trading sessions either side of an earnings date that block entries.
    earnings_block_sessions_before: int = 1
    earnings_block_sessions_after: int = 1


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
    # Net price as a fraction of the spread's STRIKE WIDTH. max_net_spread_price
    # is a single dollar cap while widths differ per symbol, so at its shipped
    # value it sat above every configured width and could not reject a quote
    # implying a credit larger than the spread itself — which books a max loss
    # of zero. This asks the question structurally instead. 0.0 disables.
    max_net_price_frac_of_width: float = 0.90
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
    # Lower VIX floor for long-premium strategies. When set > 0 and VIX
    # is below this floor at entry-decision time, ``_option_entry_block_
    # reason`` rejects with ``vix_below_floor``. Default 0.0 = filter
    # disabled (preserves legacy behaviour). Set ~12.0 for long-premium
    # strategies that suffer in dead-grind tape — typical daily range
    # at VIX < 12 (~0.4%) makes the math for 0DTE long premium thin.
    # Credit-spread strategies should leave this at 0.0 since low-VIX
    # juicy environments are by design.
    min_vix: float = 0.0
    vix_spike_pct: float = 0.0110
    # IV-rank gates (2026-05-14). Compute current VIX position within a
    # user-provided 52-week range. Rank 0.0 = at vix_52w_low, 1.0 = at
    # vix_52w_high. Long-premium strategies want low rank (cheap IV);
    # credit-spread strategies want high rank (juicy premium). Defaults
    # are disabled (min=0.0, max=1.0). Set vix_52w_low / vix_52w_high
    # in the preset yaml (refresh quarterly when VIX range shifts).
    # Implementation note: uses the 52w range as PROVIDED, not fetched
    # live — no extra Schwab calls, deterministic, user-controlled.
    vix_52w_low: float = 12.0
    vix_52w_high: float = 30.0
    min_iv_rank: float = 0.0
    max_iv_rank: float = 1.0
    vertical_limit_mode: str = "mid"
    quote_stability_checks: int = 3
    quote_stability_pause_ms: int = 500
    max_mid_drift_pct: float = 0.06
    max_quote_age_seconds: int = 6
    dry_run_replace_attempts: int = 2
    dry_run_step_frac: float = 0.25
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
    # --- Credit short-strike pivot-buffer gate (2026-05-21) ---
    # Rejects credit-spread entries where the short strike sits within
    # ``min_short_strike_pivot_buffer_atr * atr`` of the most recent
    # market-structure reference high (bear call) or reference low
    # (bull put) on either the LTF or HTF frame. The existing
    # ``min_credit_distance_atr`` only measures from current spot,
    # which can pass even when the short is essentially AT the recent
    # pivot — the 2026-05-21 session logged two such entries (SPY
    # short 741 with mshtf_reference_high 740.62 → $0.39 cushion;
    # QQQ short 712 with reference_high 711.89 → $0.11 cushion) that
    # both stopped within 30 seconds on resistance_break_exit (-$60
    # combined). Setting ``buffer = 1.0`` typically requires the
    # short strike to be at least one strike beyond the recent pivot.
    credit_pivot_buffer_gate_enabled: bool = False
    min_short_strike_pivot_buffer_atr: float = 1.0
    # --- VIX-adaptive strike width ---
    adaptive_width_enabled: bool = False
    adaptive_width_max_scale: float = 1.5


def build_schedule(entry: list[tuple[str, str]], manage: list[tuple[str, str]], screener: list[tuple[str, str]]) -> StrategySchedule:
    return StrategySchedule(
        entry_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in entry],
        management_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in manage],
        screener_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in screener],
    )


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
    events: EventsConfig = field(default_factory=EventsConfig)
    shared_entry: SharedEntryLogicConfig = field(default_factory=SharedEntryLogicConfig)
    shared_exit: SharedExitLogicConfig = field(default_factory=SharedExitLogicConfig)
    pairs: list[PairDefinition] = field(default_factory=list)

    @property
    def active_strategy(self) -> StrategyConfig:
        return self.strategies[self.strategy]

    @property
    def active_is_option(self) -> bool:
        """Whether the active strategy trades options (its manifest's
        ``plugin_type``): the one strategy-level option check, so the
        runtime reads no plugin catalogue. A signal or a position is an
        option by its own metadata (``models.is_option_asset``)."""
        return is_option_strategy(self.strategy)


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


# Keys removed from a config section, with what replaced them. A YAML still
# carrying one fails at load with the replacement named, instead of a bare
# "unexpected keyword argument" (or, for a key a dataclass would have
# swallowed, silently doing nothing): the knob's old behaviour no longer
# exists, so running on it would trade a config the operator never wrote.
_RETIRED_SECTION_KEYS: dict[str, dict[str, str]] = {
    "shared_entry": {
        "use_divergence_filter": "renamed to shared_entry.use_dual_divergence_veto (2026-09-24)",
        "use_htf_divergence_filter": "renamed to shared_entry.use_htf_divergence_score (2026-09-24)",
    },
    "technical_levels": {
        "divergence_block_dual_counter": (
            "removed 2026-09-24; the dual RSI+OBV divergence veto is "
            "shared_entry.use_dual_divergence_veto alone"
        ),
    },
    "runtime": {
        "timezone": (
            "removed 2026-09-26; the bot trades US sessions and every configured "
            "time is America/New_York (sessions.EXCHANGE_TZ)"
        ),
    },
}


# Strategy params a strategy no longer reads, per strategy, with what
# replaced them; checked against the preset's strategies.<name>.params. Each
# strategy's entry is added as it moves onto the shared entry stage
# (2026-09-24): its own veto / bypass params became shared_entry knobs or
# manifest exemptions (capabilities.shared_entry.exemptions).
_RETIRED_STRATEGY_PARAMS: dict[str, dict[str, str]] = {
    # small_cap_squeeze runs the top_tier_adaptive engine (it subclasses it),
    # so the two retire the same params.
    **{name: {
        "orb_bypass_structure_entry": (
            "replaced by the manifest exemption capabilities.shared_entry.exemptions "
            "{orb: [structure, sr]} (2026-09-24)"
        ),
        "orb_bypass_sr_entry": (
            "replaced by the manifest exemption capabilities.shared_entry.exemptions "
            "{orb: [structure, sr]} (2026-09-24)"
        ),
        "reject_entry_near_broken_level": "replaced by shared_entry.use_broken_level_guard (2026-09-24)",
        "broken_level_min_clearance_pct": "replaced by shared_entry.broken_level_min_clearance_pct (2026-09-24)",
        "broken_level_min_clearance_atr": "replaced by shared_entry.broken_level_min_clearance_atr (2026-09-24)",
    } for name in ("top_tier_adaptive", "small_cap_squeeze")},
    # The ORB path's own vetoes; the premium proposals of both styles now meet
    # the shared structure / S/R vetoes.
    "zero_dte_etf_long_options": {
        "orb_apply_structure_veto": "replaced by shared_entry.use_structure_filter (2026-09-24)",
        "orb_apply_sr_veto": "replaced by shared_entry.use_sr_filter (2026-09-24)",
    },
    # The switch that kept the shared S/R veto off for these two; the veto is
    # the global shared_entry.use_sr_filter now.
    **{name: {
        "use_sr_veto": "replaced by shared_entry.use_sr_filter (2026-09-24)",
    } for name in ("peer_confirmed_htf_pivots", "peer_confirmed_trend_continuation")},
}


def _reject_retired_keys(config_path: Path, section: str, raw: Mapping[str, Any], retired: Mapping[str, str]) -> None:
    stale = [f"{section}.{key}: {hint}" for key, hint in retired.items() if key in raw]
    if stale:
        raise ValueError(f"{config_path}: retired config keys -- " + "; ".join(stale))


def _normalize_tv_percent_param(value: Any) -> float:
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("%"):
            return float(raw[:-1].strip())
        return float(raw)
    return float(value)


def _normalize_pairs_config(values: Any) -> list[PairDefinition]:
    out: list[PairDefinition] = []
    seen: set[tuple[str, str]] = set()
    for item in values or []:
        # _section_shape_errors has refused a row that is not a mapping with
        # a symbol and a reference.
        symbol = str(item["symbol"]).upper().strip()
        reference = str(item["reference"]).upper().strip()
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


def _normalize_options_config(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw or {})
    underlyings = normalize_symbol_list(out.get("underlyings"))
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
    return out


def _normalize_strategy_params(
    params: dict[str, Any],
    strategy_name: str | None = None,
    *,
    apply_plugin_normalizer: bool = True,
) -> dict[str, Any]:
    out = dict(params or {})
    if apply_plugin_normalizer and strategy_name is not None:
        out = normalize_strategy_params(strategy_name, out)
    for key in _PERCENT_PARAM_NAMES:
        if key in out and out[key] is not None:
            out[key] = _normalize_tv_percent_param(out[key])
    return out


@dataclass(frozen=True, slots=True)
class _Number:
    """The load check for one numeric key: a YAML number (a quoted one is a
    string), never a bool (``float(True)`` is 1.0), finite, and inside the
    bounds; ``integer`` asks for an int, and ``nullable`` lets a null
    through. The readers take the value as it is, through ``float()`` /
    ``int()`` or none, mid-session: a string raised there (in the sleep
    between cycles it ended ``run()`` without its shutdown), a NaN or an
    infinity passed ``max()`` / ``min()`` clamps and comparisons silently,
    and a float was truncated."""

    integer: bool = False
    low: float | None = None
    low_open: bool = False
    high: float | None = None
    nullable: bool = False
    note: str = ""

    def error(self, key: str, value: Any) -> str | None:
        """The message naming *key* when *value* fails, else ``None``."""
        if self._accepts(value):
            return None
        kind = "an integer" if self.integer else "a finite number"
        return (f"{key} must be {kind}{self._bounds()}{self.note}, got {value!r}"
                f"{self._text_hint(value)}{self._decimal_hint(value)}")

    def _accepts(self, value: Any) -> bool:
        if value is None:
            return self.nullable
        return (
            isinstance(value, int if self.integer else int | float)
            and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value))
            and (self.low is None or (value > self.low if self.low_open else value >= self.low))
            and (self.high is None or value <= self.high)
        )

    def _text_hint(self, value: Any) -> str:
        """The hint for a number YAML read as text: a quoted one, or an
        exponent without a dot and a signed power (YAML 1.1 reads ``5e6``
        and ``1.5e6`` as strings, ``1.5e+6`` as a number). Names the value
        to write, in a form YAML reads as that number, and only when that
        number passes the check: an integer key's as an integer (``1e16``:
        ``10000000000000000``), and no hint for a number out of range
        (``'-5'`` for a count), whose unquoted form is refused too."""
        if not isinstance(value, str):
            return ""
        try:
            number = float(value)
        except ValueError:
            return ""
        if not math.isfinite(number) or (self.integer and not number.is_integer()):
            return ""
        if self.integer or (number.is_integer() and abs(number) < 1e15):
            written, parsed = str(int(number)), int(number)
        else:
            mantissa, _, power = repr(number).partition("e")
            written, parsed = f"{mantissa if '.' in mantissa else mantissa + '.0'}{'e' + power if power else ''}", number
        if not self._accepts(parsed):
            return ""
        return f" (YAML read it as text, not a number: write {written})"

    def _decimal_hint(self, value: Any) -> str:
        """The hint for a whole number written with a decimal point where an
        integer belongs (``max_positions: 2.0``, a float to YAML): names the
        integer to write, when the check takes it. Such a value loaded until
        2026-09-26, when the counts became integers."""
        if not self.integer or not isinstance(value, float) or not value.is_integer():
            return ""
        if not self._accepts(int(value)):
            return ""
        return f" (YAML read it as a decimal, not an integer: write {int(value)})"

    def _bounds(self) -> str:
        if self.low is None:
            return ""
        if self.high is None:
            return f" {'>' if self.low_open else '>='} {self.low:g}"
        return f" in {'(' if self.low_open else '['}{self.low:g}, {self.high:g}]"


_ABOVE_ZERO = _Number(low=0, low_open=True)
_AT_LEAST_ZERO = _Number(low=0)
_FRACTION = _Number(low=0, high=1)
_COUNT = _Number(integer=True, low=1)
_COUNT_OR_ZERO = _Number(integer=True, low=0)

# The numbers the engine, the data feed, the risk manager, the position
# manager, the execution layer and the dashboard read at runtime, by config
# section. load_config refuses a value outside its check, naming the key; the
# readers take the value as it is. The switches (every field a section's
# dataclass declares ``bool``) must be true or false; ``_section_errors``
# reads them off the dataclass.
_NUMBER_CHECKS: dict[str, dict[str, _Number]] = {
    "schwab": {
        "timeout": _COUNT,
    },
    "tradingview": {
        "max_candidates": _COUNT,
        "screener_refresh_seconds": _AT_LEAST_ZERO,
        "min_market_cap": _AT_LEAST_ZERO,
        "max_market_cap": _AT_LEAST_ZERO,
        "min_volume": _COUNT_OR_ZERO,
        "min_value_traded_1m": _Number(low=0, note=" (0 turns the filter off)"),
        "min_volume_1m": _Number(integer=True, low=0, note=" (0 turns the filter off)"),
    },
    "risk": {
        "max_positions": _COUNT,
        "risk_per_trade_frac_of_notional": _Number(low=0, low_open=True, high=1),
        "max_notional_per_trade": _ABOVE_ZERO,
        "max_total_notional": _ABOVE_ZERO,
        "max_daily_loss": _ABOVE_ZERO,
        "default_stop_pct": _Number(low=0, low_open=True, high=1),
        "default_target_pct": _Number(low=0, low_open=True, high=1),
        "cooldown_minutes": _COUNT_OR_ZERO,
        "same_level_block_minutes": _Number(integer=True, low=0, note=" (0 turns the block off)"),
        "same_level_block_atr_mult": _Number(low=0, note=" (0 turns the block off)"),
        "entry_slippage_allowance_spread_frac": _Number(low=0, note=" (0 sizes on the raw stop distance)"),
        "entry_slippage_allowance_max_pct": _Number(low=0, note=" (0 leaves the allowance uncapped)"),
        # Detection thresholds, read after the fill: an unreadable one raised
        # while the filled position was being booked.
        "risk_overage_warn_frac": _Number(note=" (below 0 turns the warning off)"),
        "entry_slippage_warn_pct": _Number(note=" (0 or below turns the warning off)"),
        # Read at every entry and restore (RiskManager.stock_position_trail_pct),
        # where an unreadable value switched the trail off without a word
        # until 2026-09-26, and so did a negative one.
        "trailing_stop_pct": _Number(low=0, nullable=True, note=" (0 or null turns the trail off)"),
        "time_stop_minutes": _Number(integer=True, low=0, note=" (0 turns the time stop off)"),
        "time_stop_min_return_pct": _AT_LEAST_ZERO,
        "peak_giveback_min_r": _Number(
            low=0, low_open=True, note=" (peak_giveback_enabled: false turns the giveback off)",
        ),
        "peak_giveback_low_tier_min_r": _Number(low=0, note=" (0 turns the low tier off)"),
        "peak_giveback_low_tier_giveback_frac": _FRACTION,
        "peak_giveback_retain_1to2r": _FRACTION,
        "peak_giveback_retain_2to3r": _FRACTION,
        "peak_giveback_retain_3r_plus": _FRACTION,
    },
    "runtime": {
        "loop_sleep_seconds": _ABOVE_ZERO,
        "idle_sleep_seconds": _Number(low=0, note=" (at or below loop_sleep_seconds turns the idle cadence off)"),
        "symbol_state_prune_seconds": _Number(low=0, note=" (0 turns pruning off)"),
        # Read with float() in the feed's history refresh check and the
        # warm-up retry timing, both ahead of the cycle's position management.
        "history_poll_seconds": _ABOVE_ZERO,
        "quote_poll_seconds": _ABOVE_ZERO,
        "quote_cache_seconds": _AT_LEAST_ZERO,
        "quote_batch_size": _COUNT,
        "history_lookback_minutes": _COUNT,
        "warmup_minutes": _COUNT,
        "prewarm_before_windows_minutes": _COUNT_OR_ZERO,
        "stream_connect_timeout_seconds": _COUNT,
        "stream_fallback_poll_seconds": _COUNT,
        "stream_stale_fallback_seconds": _COUNT,
        "stream_health_log_seconds": _COUNT,
        "startup_order_lookback_days": _COUNT,
        # Read every cycle by the position manager's per-position escalation
        # and on every failed cycle by the engine's. A null used to turn the
        # engine's off, and a typo raised out of its error path (2026-09-26).
        "error_escalation_cycles": _Number(integer=True, low=0, note=" (0 turns the escalation off)"),
        # The engine read ``int(value or 4)`` behind a silent except until
        # 2026-09-26.
        "cycle_precompute_workers": _COUNT,
        # Read by every quote refresh (MarketDataStore._parallel_quote_fetch),
        # where a typo used to read as 5 and null or a negative count as 0.
        "max_consecutive_quote_failures": _Number(integer=True, low=0, note=" (0 turns the gate off)"),
    },
    "paper": {
        "starting_equity": _ABOVE_ZERO,
        "max_equity_points": _COUNT,
        "max_trade_history": _COUNT,
    },
    "dashboard": {
        "port": _Number(integer=True, low=1, high=65535),
        "refresh_ms": _COUNT,
    },
    "dashboard.charting.compact": {"max_bars": _Number(integer=True, low=1, high=480)},
    "dashboard.charting.expanded": {"max_bars": _Number(integer=True, low=1, high=480)},
    # The level-spacing tolerances, read as they are by the S/R and HTF
    # builds, the strategies' HTF context and market-structure reads, the
    # dashboard and the ladder spacing (effective_side_tolerance), even with
    # support_resistance.enabled: false. Each is above 0: until 2026-09-26 a
    # 0 read as 0 in the S/R build's merge tolerance and the chart's HTF
    # request, and as a reader's own default in the others (0.35 or 0.60 ATR,
    # 0.003, 0.10 ATR, 0.0015), and a negative one read as it was, but as 0
    # in the ladder spacing. The section's other numbers are the strategies'
    # to read, and are not checked here.
    "support_resistance": {
        "atr_tolerance_mult": _ABOVE_ZERO,
        "pct_tolerance": _ABOVE_ZERO,
        "same_side_min_gap_atr_mult": _ABOVE_ZERO,
        "same_side_min_gap_pct": _ABOVE_ZERO,
    },
    # The position manager's adaptive ladder touch hold (2026-09-27): a 0
    # would time every hold out on its first cycle, before any bar could be
    # delivered. The section's switches are checked with it; its other
    # numbers are the exit policy's to read, and are not checked here.
    "shared_exit": {
        # An hour is far past any bar delivery; a timeout past pandas'
        # Timedelta range (~9.2e9 s) raised in the ladder pass mid-session.
        "adaptive_ladder_touch_hold_timeout_seconds": _Number(low=0, low_open=True, high=3600),
    },
    "execution": {
        "entry_limit_min_buffer": _AT_LEAST_ZERO,
        "entry_limit_max_buffer": _AT_LEAST_ZERO,
        "entry_limit_spread_frac": _AT_LEAST_ZERO,
        # An infinite timeout polled an unfilled order forever.
        "entry_live_fill_timeout_seconds": _ABOVE_ZERO,
        "entry_live_poll_seconds": _ABOVE_ZERO,
        "entry_live_reprice_attempts": _COUNT_OR_ZERO,
        "entry_live_reprice_step_frac": _AT_LEAST_ZERO,
        # A NaN offset priced the resting STOP_LIMIT at "nan".
        "bracket_stop_limit_offset_r": _AT_LEAST_ZERO,
        "bracket_replace_min_price_delta": _AT_LEAST_ZERO,
    },
    "events": {
        "earnings_block_sessions_before": _COUNT_OR_ZERO,
        "earnings_block_sessions_after": _COUNT_OR_ZERO,
    },
    # The options numbers the risk manager, the position manager, the
    # execution layer and the entry gatekeeper read (the 0DTE strategies read
    # the rest). The level fractions are read after the fill.
    "options": {
        "max_loss_per_trade": _ABOVE_ZERO,
        "max_contracts_per_trade": _COUNT,
        "max_quote_age_seconds": _AT_LEAST_ZERO,
        "dry_run_replace_attempts": _COUNT_OR_ZERO,
        "dry_run_step_frac": _AT_LEAST_ZERO,
        "debit_stop_frac": _ABOVE_ZERO,
        "debit_target_mult": _ABOVE_ZERO,
        "credit_stop_mult": _ABOVE_ZERO,
        "credit_target_frac": _ABOVE_ZERO,
        "single_stop_frac": _ABOVE_ZERO,
        "single_target_mult": _ABOVE_ZERO,
        "debit_stop_time_decay_widen_factor": _AT_LEAST_ZERO,
        "options_breakeven_mark_mult": _ABOVE_ZERO,
        "options_breakeven_stop_mult": _ABOVE_ZERO,
        "options_profit_lock_mark_mult": _ABOVE_ZERO,
        "options_profit_lock_stop_mult": _ABOVE_ZERO,
    },
}


# The settings that name one of a fixed set of modes, by config section, with
# the values each takes, spelled exactly so. load_config refuses any other
# value, naming the key and the values; the readers compare the value as it
# is. Until 2026-09-26 all but the bracket modes were read in any case, and
# all but startup_reconcile_mode and the two option limit modes with
# surrounding spaces ignored, so "Strict", "NATURAL", "EXTENDED" or " HTF "
# read as that mode (they are refused now); and they fell back at runtime,
# with a WARNING or without a word: a typo'd startup_reconcile_mode read as
# log_only, which turned the entry block off; an unknown
# trade_management_mode read as adaptive_ladder, and reentry_policy took six
# undocumented aliases and read an unknown value as cooldown;
# equity_session_indicator_window and compact_chart_timeframe read anything
# else as rth / ltf, order_block_mode as loose and the two option limit
# modes as mid. dashboard.theme, whose values are the theme folders, is
# checked in _validate_dashboard_config.
_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "risk": {
        "trade_management_mode": ("adaptive", "adaptive_ladder", "none", "sr_flip"),
        "reentry_policy": ("cooldown", "immediate", "rest_of_day"),
    },
    "runtime": {
        "startup_reconcile_mode": ("block", "ignore", "log_only", "restore_basic", "restore_hybrid"),
        "equity_session_indicator_window": ("extended", "rth"),
    },
    "dashboard.charting": {
        "compact_chart_timeframe": ("htf", "ltf"),
    },
    "support_resistance": {
        "order_block_mode": ("loose", "strict"),
    },
    "options": {
        "option_limit_mode": ("bid", "mid", "natural"),
        "vertical_limit_mode": ("bid", "mid", "natural"),
    },
    # A misspelling fell through to a "not that mode" branch at order-build
    # time, with a live order already in flight.
    "execution": {
        "bracket_sync_mode": ("replace", "static"),
        "bracket_legs": ("stop_and_target", "stop_only"),
        "bracket_stop_order_type": ("STOP", "STOP_LIMIT"),
    },
}

# The dashboard themes: the folders under dashboard_assets/themes whose name
# the dashboard serves (README "Custom themes"), "default" among them.
DASHBOARD_THEMES_DIR = Path(__file__).with_name("dashboard_assets") / "themes"
THEME_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")


def dashboard_themes() -> list[str]:
    """The theme names ``dashboard.theme`` takes, sorted: the folders under
    ``DASHBOARD_THEMES_DIR`` named to ``THEME_NAME_PATTERN``, so a theme a
    user drops in there is one."""
    return sorted(path.name for path in DASHBOARD_THEMES_DIR.iterdir()
                  if path.is_dir() and THEME_NAME_PATTERN.match(path.name))


def _choice_errors(section: str, cfg: Any) -> list[str]:
    """``section``'s modes (``_CHOICES``) that are none of their values."""
    return [
        f"{section}.{key} must be one of {sorted(choices)}, got {getattr(cfg, key)!r}"
        for key, choices in _CHOICES.get(section, {}).items()
        if getattr(cfg, key) not in choices
    ]


def _number_errors(section: str, cfg: Any) -> list[str]:
    """``section``'s numbers (``_NUMBER_CHECKS``) that fail their check."""
    return [
        error for key, check in _NUMBER_CHECKS.get(section, {}).items()
        if (error := check.error(f"{section}.{key}", getattr(cfg, key))) is not None
    ]


def _section_errors(section: str, cfg: Any) -> list[str]:
    """``section``'s numbers (``_NUMBER_CHECKS``), modes (``_CHOICES``) and
    switches: every field its dataclass declares ``bool`` must be true or
    false. YAML reads ``yes`` / ``no`` / ``on`` / ``off`` as booleans too;
    the string ``"false"`` is truthy and a null is falsy, so either flipped
    the switch without a word (a blank ``schwab.dry_run:`` traded live)."""
    errors = _number_errors(section, cfg)
    errors += _choice_errors(section, cfg)
    for spec in fields(cfg):
        value = getattr(cfg, spec.name)
        if spec.type == "bool" and not isinstance(value, bool):
            errors.append(f"{section}.{spec.name} must be true or false, got {value!r}")
    return errors


def _raise_section_errors(section: str, errors: list[str], config_path: Path) -> None:
    if errors:
        raise ValueError(f"{config_path}: invalid {section} configuration:\n  " + "\n  ".join(errors))


def _validate_risk_config(risk: RiskConfig, config_path: Path) -> None:
    """The risk numbers, modes and switches (``_NUMBER_CHECKS["risk"]``,
    ``_CHOICES["risk"]``): a negative max_daily_loss inverted the daily
    loss check, zero max_positions blocked every entry, and a typo raised in
    the entry or management cycle."""
    _raise_section_errors("risk", _section_errors("risk", risk), config_path)


def _validate_runtime_config(runtime: RuntimeConfig, config_path: Path) -> None:
    """The runtime cadence, cache, stream and reconcile numbers, modes and
    switches (``_NUMBER_CHECKS["runtime"]``, ``_CHOICES["runtime"]``).

    These fields previously had scattered getattr(..., default) fallbacks in
    call sites (engine.py, data_feed.py, execution.py) that silently papered
    over bad or missing values. With those removed, we validate at load time
    so misconfiguration fails loudly up front. The two sleeps and the prune
    cadence are read between cycles, outside the cycle's error handling: an
    unreadable, NaN or infinite value there ended ``run()`` without its
    shutdown (2026-09-26)."""
    errors = _section_errors("runtime", runtime)
    # Iterated as symbols: a string was read letter by letter, so the symbol
    # it named was never ignored, and an unquoted ON (read as true) as TRUE.
    # One message per bad entry, as for an event row's symbols.
    ignore = runtime.startup_reconcile_ignore_symbols
    if ignore is not None and not isinstance(ignore, list):
        errors.append(f"runtime.startup_reconcile_ignore_symbols must be a list of tickers, got {ignore!r}")
    elif ignore is not None:
        errors += [
            f"runtime.startup_reconcile_ignore_symbols[{index}] must be a ticker, got {symbol!r}"
            f"{ticker_quote_hint(symbol)}"
            for index, symbol in enumerate(ignore)
            if not isinstance(symbol, str) or not symbol.strip()
        ]
    _raise_section_errors("runtime", errors, config_path)


def _validate_support_resistance_config(sr: SupportResistanceConfig, config_path: Path) -> None:
    """Reject flip-confirmation bar counts below 0, and both at 0. 0 is valid
    for one frame (that frame's gate is off, see ``SupportResistanceConfig.flip_confirmation_bars``);
    a negative count has no meaning and would read as "never confirms" deep
    inside confirm_by_bars. With both off the readers disagreed: the S/R
    builders fell back to the last HTF bar, while the key-levels ladder
    defence (``_ladder_exit_signal``) has no fallback bar and could never
    confirm, so it never fired.

    The four level-spacing tolerances (``_NUMBER_CHECKS``) must be finite
    YAML numbers above 0, even with ``enabled: false``. The S/R and HTF
    builders and the dashboard read them with ``float()``, so a typo raised
    in every build; the ladder spacing (sr_flip management and the dashboard
    ladder, until 2026-09-27 ``_sr_ladder``, now
    ``levels_shared.effective_side_tolerance``) read it as the default. ``order_block_mode`` is
    ``loose`` or ``strict`` (``_CHOICES``)."""
    errors = _number_errors("support_resistance", sr) + _choice_errors("support_resistance", sr)
    names = ("trading_flip_confirmation_1m_bars", "trading_flip_confirmation_5m_bars")
    for name in names:
        value = getattr(sr, name)
        if int(value) < 0:
            errors.append(f"support_resistance.{name} must be >= 0, got {value}")
    if all(int(getattr(sr, name)) == 0 for name in names):
        errors.append("support_resistance: at least one of trading_flip_confirmation_1m_bars / _5m_bars must be > 0")
    if errors:
        raise ValueError(f"{config_path}: invalid support_resistance configuration:\n  " + "\n  ".join(errors))


def _validate_execution_config(execution: EquityExecutionConfig, risk: RiskConfig,
                               shared_exit: SharedExitLogicConfig, config_path: Path) -> None:
    """Plausibility checks for the equity execution / bracket-order block:
    the numbers, the bracket modes and the switches (``_NUMBER_CHECKS`` /
    ``_CHOICES["execution"]``), and the bracket combinations the risk
    management mode and the adaptive ladder's touch hold rule out
    (``shared_exit`` is checked before this)."""
    errors = _section_errors("execution", execution)
    if execution.bracket_orders_enabled is True:  # anything but a bool is refused above
        # A resting target limit fills at the touched rung, through the
        # adaptive ladder's touch hold, so the hold could never promote a
        # rung or run past the last one. Without the hold the ladder's first
        # rung is a plain take-profit, which a resting target serves as well
        # (until 2026-09-27 this refused every adaptive_ladder bracket, for
        # the target-exit suppression and the final-rung runner, removed).
        if (risk.trade_management_mode == "adaptive_ladder" and shared_exit.adaptive_ladder_touch_hold is True
                and execution.bracket_legs == "stop_and_target"):
            errors.append(
                "execution.bracket_legs must be 'stop_only' when "
                "shared_exit.adaptive_ladder_touch_hold is on with "
                "risk.trade_management_mode 'adaptive_ladder': a resting target "
                "limit fills at the touched rung through the hold, so the ladder "
                "could never promote a rung or run past the last one"
            )
        # static sync + an engine that ratchets stops = the broker holds a
        # stale protective level for the life of the trade. Every breakeven /
        # profit-lock / trail move would be invisible to the resting order.
        if execution.bracket_sync_mode == "static" and risk.trade_management_mode in {"adaptive", "adaptive_ladder"}:
            errors.append(
                f"execution.bracket_sync_mode 'static' cannot be used with "
                f"risk.trade_management_mode {risk.trade_management_mode!r}: the engine "
                "ratchets stop_price in-trade and static mode never replaces the "
                "resting child, leaving the broker on the entry-time stop. Use "
                "bracket_sync_mode: replace, or a non-adaptive management mode"
            )
    _raise_section_errors("execution", errors, config_path)


def _validate_shared_exit_config(shared_exit: SharedExitLogicConfig, risk: RiskConfig, config_path: Path) -> None:
    """The shared exit switches, and the adaptive ladder touch hold's
    timeout (``_NUMBER_CHECKS["shared_exit"]``). ``SharedExitPolicy`` reads
    a switch with ``bool()``, so a quoted ``"false"`` turned an exit family
    (or the touch hold) on and a blank one off. The touch hold acts only on
    a laddered position, so it is refused unless
    ``risk.trade_management_mode`` is ``adaptive_ladder``: on under another
    mode it would do nothing without a word."""
    errors = _section_errors("shared_exit", shared_exit)
    if shared_exit.adaptive_ladder_touch_hold is True and risk.trade_management_mode != "adaptive_ladder":
        errors.append("shared_exit.adaptive_ladder_touch_hold acts only on laddered positions: it needs "
                      f"risk.trade_management_mode adaptive_ladder, got {risk.trade_management_mode!r}")
    _raise_section_errors("shared_exit", errors, config_path)


def _validate_options_config(options: "ZeroDteOptionsConfig", config_path: Path) -> None:
    """Plausibility checks for options sizing, quote-freshness, the levels,
    the switches and the times (``_NUMBER_CHECKS["options"]``).

    max_quote_age_seconds was previously read via ``getattr(..., 10)``
    fallback in the engine before Phase 1 validators landed; validation
    replaces that silent default."""
    errors = _section_errors("options", options)
    if options.underlyings is not None and not isinstance(options.underlyings, list):
        errors.append(f"options.underlyings must be a list of symbols, got {options.underlyings!r}")
    # The times fail here, naming the key, instead of where they are read:
    # force_flatten_time when an options strategy is built, the other three
    # at their first read, mid-session. A blank force_flatten_time meant
    # 15:18 until 2026-09-26 (a normalizer, removed as a silent fallback).
    for name in ("force_flatten_time", "debit_target_time_decay_start", "debit_target_time_decay_end",
                 "delta_time_shift_start"):
        value = getattr(options, name)
        if not is_hhmm(value):
            errors.append(f"options.{name} must be an HH:MM time, got {value!r}")
    _raise_section_errors("options", errors, config_path)


def _validate_events_config(events: EventsConfig, config_path: Path) -> None:
    """``events.blackouts`` must be a list of mappings whose ``start`` /
    ``end`` are HH:MM times (and whose other fields read, see
    ``blackout_row_errors``), ``events.earnings`` a
    ``{SYMBOL: [YYYY-MM-DD, ...]}`` map, ``enabled`` a switch and the two
    earnings session counts integers >= 0.

    The calendar parses a row's times only on the row's date, inside the
    entry and (for the 0DTE strategies) force-flatten checks, so a typo
    waited for the event itself; a scalar where a list belongs raised a bare
    TypeError, and a bad earnings date was dropped with a warning. The two
    files are checked when the strategy builds its calendar
    (``event_blackouts.EventBlackoutCalendar``)."""
    errors = _section_errors("events", events)
    errors += blackout_row_errors(events.blackouts, "events.blackouts")
    errors += earnings_errors(events.earnings, "events.earnings")
    _raise_section_errors("events", errors, config_path)


def _validate_dashboard_config(dashboard: DashboardConfig, config_path: Path) -> None:
    """The dashboard's port, refresh, theme and switches, the compact
    chart's timeframe, and each chart profile's ``max_bars`` (1-480) and
    switches. The server read the port and the refresh with ``int()`` when
    the bot was built; it lowercased and stripped the theme, so ``Nebula``
    or `` dark `` served that theme, and read one that was malformed or
    named no folder there as ``default``, with a WARNING (a null without
    one); a chart profile read an unreadable ``max_bars`` as its default
    and any string as a switch that is on."""
    errors = _section_errors("dashboard", dashboard)
    themes = dashboard_themes()
    if dashboard.theme not in themes:
        errors.append(
            f"dashboard.theme must be one of the theme folders under {DASHBOARD_THEMES_DIR}, {themes}, "
            f"got {dashboard.theme!r}"
        )
    errors += _section_errors("dashboard.charting", dashboard.charting)
    for name in ("compact", "expanded"):
        errors += _section_errors(f"dashboard.charting.{name}", getattr(dashboard.charting, name))
    _raise_section_errors("dashboard", errors, config_path)


def _charting_key_errors(charting: Mapping[str, Any], compact: Mapping[str, Any],
                         expanded: Mapping[str, Any]) -> list[str]:
    """The keys under ``dashboard.charting`` and its two chart profiles that
    none of them takes. ``load_config`` splits the section by hand, so an
    unknown key (a typo such as ``compact_timeframe:``) was ignored without
    a word until 2026-09-26, leaving the setting it meant at its default;
    one in a profile raised the dataclass's bare "unexpected keyword
    argument". The retired ``shared`` profile and top-level ``*_max_bars``
    keys, which had messages of their own, are unknown keys like any
    other."""
    taken = [spec.name for spec in fields(DashboardChartingConfig)]
    profile = [spec.name for spec in fields(DashboardChartConfig)]
    errors = [f"dashboard.charting has an unknown key {key!r}: it takes {', '.join(taken)}"
              for key in charting if key not in taken]
    for name, raw in (("compact", compact), ("expanded", expanded)):
        errors += [f"dashboard.charting.{name} has an unknown key {key!r}: a chart profile takes {', '.join(profile)}"
                   for key in raw if key not in profile]
    return errors


def _validate_section(section: str, cfg: Any, config_path: Path) -> None:
    """A section with only numbers and switches to check: ``schwab``
    (``timeout``, and ``dry_run``, which a blank or ``0`` read as false:
    live trading), ``tradingview`` and ``paper``."""
    _raise_section_errors(section, _section_errors(section, cfg), config_path)


def _validate_strategy_windows(strategies: dict[str, "StrategyConfig"], config_path: Path) -> None:
    """Each ``entry_windows`` / ``management_windows`` / ``screener_windows``
    entry must be a ``[start, end]`` pair of HH:MM times.

    The manifests' windows are checked when the catalogue loads; these are
    the YAML overrides, which ``build_schedule`` parsed only in the engine's
    first cycle (and again in its error handler, so the bot died there)."""
    errors: list[str] = []
    for name, cfg in strategies.items():
        for field_name in ("entry_windows", "management_windows", "screener_windows"):
            windows = getattr(cfg, field_name)
            if not isinstance(windows, list | tuple):
                errors.append(f"strategies.{name}.{field_name} must be a list of [start, end] windows, got {windows!r}")
                continue
            for index, window in enumerate(windows):
                if isinstance(window, list | tuple) and len(window) == 2 and all(is_hhmm(end) for end in window):
                    continue
                errors.append(
                    f"strategies.{name}.{field_name}[{index}] must be a [start, end] pair of HH:MM times, "
                    f"got {window!r}"
                )
    if errors:
        raise ValueError(f"{config_path}: invalid strategy windows:\n  " + "\n  ".join(errors))


def _validate_sector_index_map(strategies: dict[str, "StrategyConfig"], config_path: Path) -> None:
    """Reject a ``sector_index_map`` that references un-streamed ETFs.

    ``TopTierAdaptiveStrategy.active_watchlist`` subscribes bars for the
    ``index_symbols`` list and nothing else, while ``_indices_for_symbol``
    resolves a candidate's sector through ``sector_index_map``. When those
    two disagree, ``_index_confirms`` calls ``bars.get(sym)``, gets None for
    every mapped ETF, falls through the loop and returns False — so every
    candidate in that sector is permanently blocked from the five
    momentum-family regimes (trend / pullback / vol_squeeze / momentum /
    vwap_reclaim). The only symptom is a ``..._index_not_confirmed`` skip
    line, indistinguishable from the index genuinely disagreeing.

    Only sectors that actually have members are checked: mapping a sector
    you have not populated yet is harmless, and configs routinely carry a
    full 11-GICS map against a narrower traded universe.
    """
    errors: list[str] = []
    for name, cfg in strategies.items():
        params = cfg.params or {}
        sector_groups = params.get("sector_groups") or {}
        sector_index_map = params.get("sector_index_map") or {}
        if not sector_groups or not sector_index_map:
            continue
        streamed = {
            str(s).upper().strip()
            for s in (params.get("index_symbols") or [])
            if str(s).strip()
        }
        for sector, members in sector_groups.items():
            if not isinstance(members, (list, tuple)) or not members:
                continue
            mapped = sector_index_map.get(sector) or []
            missing = sorted(
                {str(s).upper().strip() for s in mapped if str(s).strip()} - streamed
            )
            if missing:
                errors.append(
                    f"strategies.{name}.sector_index_map[{sector!r}] references "
                    f"{missing} which are absent from index_symbols, so their bars are "
                    f"never streamed — every symbol in that sector "
                    f"({[str(m) for m in members][:4]}...) would be permanently blocked "
                    "from the index-confirmed regimes. Add them to index_symbols."
                )
    if errors:
        raise ValueError(
            f"{config_path}: invalid sector index mapping:\n  " + "\n  ".join(errors)
        )


# The top-level sections load_config reads as mappings of settings; ``pairs``
# is a list of pair rows.
_MAPPING_SECTIONS = ("schwab", "tradingview", "risk", "runtime", "paper", "dashboard", "execution", "candles",
                     "chart_patterns", "support_resistance", "technical_levels", "events", "shared_entry",
                     "shared_exit", "options", "strategies")


def _section_shape_errors(raw: Mapping[str, Any]) -> list[str]:
    """One message per section, ``dashboard.charting`` or chart profile,
    strategy entry or its ``params`` that is not a mapping (a null one is
    empty), and for a ``pairs`` that is not a list. Until 2026-09-26 each
    was read with ``dict(value or {})`` (a strategy entry with ``.get``):
    ``risk: 5`` raised a bare "'int' object is not iterable" and
    ``compact: [1]`` a bare "cannot convert dictionary update sequence
    element", naming neither the section nor the file, ``strategies:
    {top_tier_adaptive: 5}`` (or ``null``) a bare AttributeError; a section
    left as ``0``, ``""``, ``[]`` or ``false`` read as an empty one, so every
    key in it ran on its default, a list of ``[key, value]`` pairs read as a
    mapping, and ``pairs: {...}`` as no pairs. A pair row that is not a
    mapping with a symbol and a reference was dropped without a word, and a
    top-level key that names no section was ignored."""
    errors: list[str] = []

    def mapping(where: str, value: Any, what: str = "a mapping of settings") -> Mapping[str, Any]:
        """*value* when it is a mapping; else empty, with a message unless
        it is null."""
        if value is not None and not isinstance(value, dict):
            errors.append(f"{where} must be {what}, got {value!r}")
        return value if isinstance(value, dict) else {}

    sections = {
        section: mapping(section, raw.get(section), "a mapping of strategy names to their settings"
                         if section == "strategies" else "a mapping of settings")
        for section in _MAPPING_SECTIONS
    }
    charting = mapping("dashboard.charting", sections["dashboard"].get("charting"))
    for name in ("compact", "expanded"):
        mapping(f"dashboard.charting.{name}", charting.get(name))
    for name, entry in sections["strategies"].items():
        mapping(f"strategies.{name}.params", mapping(f"strategies.{name}", entry).get("params"))
    pairs = raw.get("pairs")
    if pairs is not None and not isinstance(pairs, list):
        errors.append(f"pairs must be a list of pair rows, got {pairs!r}")
    for index, row in enumerate(pairs if isinstance(pairs, list) else []):
        if not isinstance(row, dict) or not all(str(row.get(key) or "").strip() for key in ("symbol", "reference")):
            errors.append(f"pairs[{index}] must be a mapping with a symbol and a reference, got {row!r}")
    # A misspelled section (``risks:``, ``support_resistence:``) used to
    # load, and every key under it ran on its default.
    known = set(_MAPPING_SECTIONS) | {"strategy", "pairs"}
    for key in raw:
        if key not in known:
            errors.append(f"unknown section {key!r}: the config takes {', '.join(sorted(known))}")
    return errors


def _settings(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    """A copy of the mapping under *key* (``_section_shape_errors`` has
    refused one that is not); a null or absent one is empty."""
    value = raw.get(key)
    return {} if value is None else dict(value)


def load_config(path: str | Path, strategy_override: str | None = None, env_path: str | Path | None = None) -> BotConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Copy configs/config.example.yaml to configs/config.yaml or pass --config with a valid YAML path."
        )
    # Read as the event calendar reads its files (serialization.read_yaml):
    # an unquoted date that cannot exist (events.earnings AAPL: [2026-11-31])
    # raised PyYAML's bare "day is out of range for month", naming neither
    # the file nor the line, and a YAML syntax error did not name the file
    # (2026-09-26). An empty file (or one of comments only) is refused: it
    # ran on the code defaults, a config the operator never wrote.
    raw = read_yaml(config_path)
    if raw is None:
        raise ValueError(f"{config_path}: the config file is empty; it must be a mapping of config sections")
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: must be a mapping of config sections, got {raw!r}")
    shape_errors = _section_shape_errors(raw)
    if shape_errors:
        raise ValueError(f"{config_path}: invalid config sections:\n  " + "\n  ".join(shape_errors))

    # Load .env (if present) before resolving secrets. Process env always
    # wins; .env only fills in keys that aren't already set. If env_path
    # is provided (via --env CLI flag) it's loaded with priority and
    # missing-file becomes a hard error.
    explicit_env = Path(env_path).expanduser() if env_path else None
    _load_dotenv(config_path, explicit_env_path=explicit_env)

    strategy = normalize_strategy_name(strategy_override or raw.get("strategy", default_strategy_name()))
    raw["strategy"] = strategy
    schwab_raw = _settings(raw, "schwab")
    tv_raw = _settings(raw, "tradingview")
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
    risk_raw = _settings(raw, "risk")
    runtime_raw = _settings(raw, "runtime")
    paper_raw = _settings(raw, "paper")
    dashboard_raw = _settings(raw, "dashboard")
    dashboard_charting_raw = _settings(dashboard_raw, "charting")
    dashboard_raw.pop("charting", None)
    compact_charting_raw = _settings(dashboard_charting_raw, "compact")
    expanded_charting_raw = _settings(dashboard_charting_raw, "expanded")
    _raise_section_errors("dashboard", _charting_key_errors(
        dashboard_charting_raw, compact_charting_raw, expanded_charting_raw), config_path)
    execution_raw = _settings(raw, "execution")
    candles_raw = _settings(raw, "candles")
    chart_patterns_raw = _settings(raw, "chart_patterns")
    _validate_pattern_config(config_path, candles_raw, chart_patterns_raw)
    support_resistance_raw = _settings(raw, "support_resistance")
    technical_levels_raw = _settings(raw, "technical_levels")
    events_raw = _settings(raw, "events")
    shared_entry_raw = _settings(raw, "shared_entry")
    shared_exit_raw = _settings(raw, "shared_exit")
    for section, section_raw in (("shared_entry", shared_entry_raw), ("technical_levels", technical_levels_raw),
                                 ("runtime", runtime_raw)):
        _reject_retired_keys(config_path, section, section_raw, _RETIRED_SECTION_KEYS[section])
    options_raw = _normalize_options_config(_settings(raw, "options"))

    strategies = _strategy_defaults()

    strategies_raw = _settings(raw, "strategies")
    for key in strategies_raw:
        name = normalize_strategy_name(key)
        value = _settings(strategies_raw, key)
        params_raw = _settings(value, "params")
        _reject_retired_keys(config_path, f"strategies.{name}.params", params_raw,
                             _RETIRED_STRATEGY_PARAMS.get(name, {}))
        base = strategies[name]
        merged_params = deepcopy(base.params)
        merged_params.update(deepcopy(params_raw))
        strategies[name] = StrategyConfig(
            name=name,
            entry_windows=deepcopy(value.get("entry_windows", base.entry_windows)),
            management_windows=deepcopy(value.get("management_windows", base.management_windows)),
            screener_windows=deepcopy(value.get("screener_windows", base.screener_windows)),
            params=_normalize_strategy_params(merged_params, name),
        )

    _validate_sector_index_map(strategies, config_path)
    _validate_strategy_windows(strategies, config_path)

    active_base = strategies[strategy]
    strategies[strategy] = StrategyConfig(
        name=active_base.name,
        entry_windows=deepcopy(active_base.entry_windows),
        management_windows=deepcopy(active_base.management_windows),
        screener_windows=deepcopy(active_base.screener_windows),
        params=deepcopy(active_base.params),
    )

    pairs = _normalize_pairs_config(raw.get("pairs", []))

    schwab_cfg = SchwabConfig(**schwab_raw)
    _validate_section("schwab", schwab_cfg, config_path)
    tradingview_cfg = TradingViewConfig(**tv_raw)
    _validate_section("tradingview", tradingview_cfg, config_path)
    paper_cfg = PaperConfig(**paper_raw)
    _validate_section("paper", paper_cfg, config_path)

    runtime_cfg = RuntimeConfig(**runtime_raw)
    _validate_runtime_config(runtime_cfg, config_path)
    set_runtime_indicator_mode(runtime_cfg.use_rth_session_indicators)
    set_session_indicator_window(runtime_cfg.equity_session_indicator_window)

    risk_cfg = RiskConfig(**risk_raw)
    _validate_risk_config(risk_cfg, config_path)

    shared_exit_cfg = SharedExitLogicConfig(**shared_exit_raw)
    _validate_shared_exit_config(shared_exit_cfg, risk_cfg, config_path)

    execution_cfg = EquityExecutionConfig(**execution_raw)
    _validate_execution_config(execution_cfg, risk_cfg, shared_exit_cfg, config_path)

    options_cfg = ZeroDteOptionsConfig(**options_raw)
    _validate_options_config(options_cfg, config_path)
    if is_option_strategy(strategy) and not options_cfg.underlyings:
        raise ValueError(
            f"{config_path}: active strategy {strategy!r} is an options strategy "
            "but options.underlyings is empty. Add at least one underlying symbol "
            "(e.g. SPY, QQQ) under the options section."
        )

    support_resistance_cfg = SupportResistanceConfig(**support_resistance_raw)
    _validate_support_resistance_config(support_resistance_cfg, config_path)

    events_cfg = EventsConfig(**events_raw)
    _validate_events_config(events_cfg, config_path)

    dashboard_cfg = DashboardConfig(
        **dashboard_raw,
        charting=DashboardChartingConfig(
            compact_chart_timeframe=dashboard_charting_raw.get("compact_chart_timeframe", "ltf"),
            compact=DashboardChartConfig(**compact_charting_raw),
            expanded=DashboardChartConfig(**expanded_charting_raw),
        ),
    )
    _validate_dashboard_config(dashboard_cfg, config_path)

    return BotConfig(
        strategy=strategy,
        schwab=schwab_cfg,
        tradingview=tradingview_cfg,
        risk=risk_cfg,
        runtime=runtime_cfg,
        paper=paper_cfg,
        dashboard=dashboard_cfg,
        execution=execution_cfg,
        candles=CandlesConfig(**candles_raw),
        chart_patterns=ChartPatternsConfig(**chart_patterns_raw),
        support_resistance=support_resistance_cfg,
        technical_levels=TechnicalLevelsConfig(**technical_levels_raw),
        events=events_cfg,
        shared_entry=SharedEntryLogicConfig(**shared_entry_raw),
        shared_exit=shared_exit_cfg,
        options=options_cfg,
        strategies=strategies,
        pairs=pairs,
    )
