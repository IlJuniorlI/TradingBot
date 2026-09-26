# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "intraday_tv_schwab_bot" / "_strategies"
CONFIGS_DIR = ROOT / "configs"
CANONICAL_TEMPLATE = CONFIGS_DIR / "config.example.yaml"

# The script runs from a checkout (python scripts/scaffold_strategy_plugin.py),
# not an installed package; a scaffolded preset's shared sections are read
# from the config dataclasses themselves.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intraday_tv_schwab_bot.config import (  # noqa: E402
    RiskConfig,
    SharedEntryLogicConfig,
    SharedExitLogicConfig,
    SupportResistanceConfig,
)


def _snake_case(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip())
    token = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", token)
    token = re.sub(r"_+", "_", token).strip("_").lower()
    if not token:
        raise ValueError("strategy name must contain at least one letter or number")
    return token


def _camel_case(value: str) -> str:
    parts = [part for part in _snake_case(value).split("_") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _default_class_stem(name: str) -> str:
    plugin_name = _snake_case(name)
    if plugin_name.endswith("_strategy"):
        trimmed = plugin_name[: -len("_strategy")]
        plugin_name = trimmed or plugin_name
    return _camel_case(plugin_name)


# ---------------------------------------------------------------------------
# Shared FVG entry-adjustment param defaults. Declared explicitly in scaffolded
# manifests so the FVG score term the shared entry policy computes for every
# admitted proposal reads documented manifest values instead of hardcoded
# fallbacks. Defaults mirror
# SharedEntryPolicy._fvg_entry_adjustment_components in
# _strategies/shared_entry.py — search for that method to verify if you tune
# them (line numbers drift as the file grows).
_FVG_PARAMS = {
    "htf_fvg_entry_weight": 0.55,
    "ltf_fvg_entry_weight": 0.35,
    "opposing_fvg_entry_penalty_mult": 1.0,
    "fvg_runner_rr_bonus": 0.12,
    "same_direction_fvg_validated_bonus": 0.15,
    "same_direction_fvg_active_bonus": 0.12,
    "opposing_fvg_validated_penalty": 0.15,
    "opposing_fvg_active_penalty": 0.12,
    "invalidated_opposing_fvg_bonus": 0.10,
    "same_direction_fvg_invalidated_penalty": 0.102,
}


def _stock_manifest_params() -> dict[str, Any]:
    return {
        "symbols": ["SPY", "QQQ"],
        "min_bars": 40,
        "min_rvol": 1.5,
        **_FVG_PARAMS,
        # End-of-window force-flatten policy. Read by
        # _configurable_stock_force_flatten (called from the scaffolded
        # should_force_flatten override). Both directions default on so
        # positions don't survive past the management window without
        # explicit opt-out.
        "force_flatten": {"long": True, "short": True},
    }


def _option_manifest_params() -> dict[str, Any]:
    return {
        # Option strategies read tradable underlyings from options.underlyings,
        # not from params.symbols. The screener synthesizes candidates from
        # that list directly (no TradingView call).
        "min_bars": 40,
        **_FVG_PARAMS,
    }


def _stock_manifest_capabilities() -> dict[str, Any]:
    return {
        "dashboard": {
            "tradable_symbols_source": "params.symbols",
        },
        "startup_restore": {
            "eligible_symbols_source": "dashboard_tradable_symbols",
            "require_hybrid_metadata": False,
        },
        "watchlist": {
            "active_sources": [
                "candidates",
                "positions.underlyings_or_symbols",
                "positions.reference_symbols",
                "dashboard_tradable_symbols",
            ],
            "quote_sources": ["active_watchlist"],
        },
        "history": {
            "required_bars": 40,
        },
    }


def _option_manifest_capabilities(name: str) -> dict[str, Any]:
    return {
        "dashboard": {
            "tradable_symbols_source": "options.underlyings",
        },
        "startup_restore": {
            "eligible_symbols_source": "dashboard_tradable_symbols",
            "require_hybrid_metadata": False,
        },
        "watchlist": {
            "active_sources": [
                "candidates",
                "options.underlyings",
                "options.confirmation_symbols",
                "options.volatility_symbol",
                {
                    "source": "positions.metadata",
                    "key": "underlying",
                    "strategy_names": [name],
                },
                {
                    "source": "positions.metadata",
                    "key": "confirm_index",
                    "strategy_names": [name],
                },
            ],
            "quote_sources": [
                "options.volatility_symbol",
                "options.confirmation_symbols",
                {
                    "source": "positions.metadata_list",
                    "key": "valuation_legs",
                    "strategy_names": [name],
                },
            ],
        },
        "history": {
            "required_bars": 40,
        },
    }


def _manifest(name: str, class_stem: str, plugin_type: str) -> str:
    is_option = plugin_type == "option"
    return json.dumps(
        {
            "schema_version": 1,
            "name": name,
            "type": plugin_type,
            "strategy_module": f"intraday_tv_schwab_bot._strategies.{name}.strategy",
            "strategy_class": f"{class_stem}Strategy",
            "screener_module": f"intraday_tv_schwab_bot._strategies.{name}.screener",
            "screener_class": f"{class_stem}Screener",
            "entry_windows": [["09:35", "14:00"]] if is_option else [["09:35", "11:30"]],
            "management_windows": [["09:30", "15:15"]] if is_option else [["09:30", "15:55"]],
            "screener_windows": [["09:35", "14:00"]] if is_option else [["09:35", "11:30"]],
            "params": _option_manifest_params() if is_option else _stock_manifest_params(),
            "capabilities": _option_manifest_capabilities(name) if is_option else _stock_manifest_capabilities(),
        },
        indent=2,
    ) + "\n"


def _stock_strategy_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        # SPDX-License-Identifier: MIT
        import pandas as pd

        from ...models import Candidate, Position, Side, Signal
        from ...numeric import safe_float
        from ...reasons import insufficient_bars_reason
        from ..shared_entry import EntryProposal
        from ..strategy_base import BaseStrategy


        class {class_stem}Strategy(BaseStrategy):
            """Stock strategy scaffold.

            Every entry goes through the shared entry stage: build an
            ``EntryProposal`` from the setup, ``self.entry_policy.admit`` it
            (it applies every shared_entry knob -- the structure / S/R /
            chart / candle / divergence vetoes, the stop/target refinement,
            the score terms -- and records a refusal under the proposal's
            style), then ``self.entry_policy.emit`` the Signal. A strategy
            never builds a Signal itself and never reads config.shared_entry
            (tests/test_shared_knob_contract.py). See
            _strategies/mean_reversion for a worked example.
            """

            strategy_name = {name!r}

            def entry_signals(
                self,
                candidates: list[Candidate],
                bars: dict[str, pd.DataFrame],
                positions: dict[str, Position],
                client=None,
                data=None,
            ) -> list[Signal]:
                self._reset_entry_decisions()
                out: list[Signal] = []
                min_bars = int(self.params.get("min_bars", 40) or 40)
                min_rvol = float(self.params.get("min_rvol", 1.5) or 1.5)
                allow_short = bool(self.config.risk.allow_short)

                for candidate in candidates:
                    if candidate.symbol in positions:
                        self._record_entry_decision(candidate.symbol, "skipped", ["already_in_position"])
                        continue

                    frame = bars.get(candidate.symbol)
                    if frame is None or len(frame) < min_bars:
                        self._record_entry_decision(
                            candidate.symbol,
                            "skipped",
                            [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)],
                        )
                        continue

                    last = frame.iloc[-1]
                    close = safe_float(last.get("close"), 0.0)
                    vwap = safe_float(last.get("vwap"), close)
                    rvol = safe_float(candidate.metadata.get("relative_volume_10d_calc"), 0.0)
                    day_strength = safe_float(candidate.metadata.get("change_from_open"), 0.0)

                    side = Side.SHORT if (allow_short and close < vwap and day_strength < 0) else Side.LONG
                    # The setup's own blockers. They are the proposal's
                    # pending reasons, so a refusal lists them together with
                    # every shared veto that fired.
                    reasons: list[str] = []
                    if rvol < min_rvol:
                        reasons.append("rvol_too_low")
                    if side == Side.LONG and not (close > vwap and day_strength > 0):
                        reasons.append("no_setup")

                    proposal = EntryProposal(
                        candidate=candidate,
                        direction=side,
                        style="trend",
                        style_family="trend",
                        close=close,
                        stop=close * (0.995 if side == Side.LONG else 1.005),
                        target=close * (1.010 if side == Side.LONG else 0.990),
                        gate_frame=frame,
                        sr_frame=frame,
                        level_frame=frame,
                        data=data,
                        pending_reasons=tuple(reasons),
                    )
                    admitted = self.entry_policy.admit(proposal)
                    if admitted is None:
                        refusal = self._consume_build_failure_payload(candidate.symbol, proposal.style)
                        self._record_entry_decision(candidate.symbol, "skipped", refusal["reasons"])
                        continue

                    # Management and score come from the ADMITTED levels (the
                    # policy may have refined the stop and target).
                    management = self._adaptive_management_components(
                        side,
                        close,
                        admitted.stop,
                        admitted.target,
                        style="trend",
                        runner_allowed=False,
                        continuation_bias=float(admitted.fvg["fvg_continuation_bias"]),
                    )
                    # The strategy's own priority, without the shared score
                    # terms: emit adds them (final_priority_score).
                    strategy_score = 1.0 + float(candidate.activity_score) * 0.15
                    signal = self.entry_policy.emit(
                        admitted,
                        reason={name!r},
                        strategy_score=strategy_score,
                        management=management,
                        target=admitted.target,
                        metadata={{
                            "rvol": rvol,
                            "day_strength": day_strength,
                            "activity_score": float(candidate.activity_score),
                        }},
                    )
                    self._record_entry_decision(candidate.symbol, "signal", [signal.reason])
                    out.append(signal)
                return out

            def should_force_flatten(self, position: Position) -> bool:
                return self._configurable_stock_force_flatten(position)
        '''
    ).lstrip()


def _option_strategy_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        # SPDX-License-Identifier: MIT
        import pandas as pd

        from ...models import Candidate, Position, Side, Signal
        from ...bars import session_open_price
        from ...numeric import safe_float
        from ...reasons import insufficient_bars_reason
        from ..strategy_base import BaseStrategy
        from ... import sessions


        class {class_stem}Strategy(BaseStrategy):
            """Option strategy scaffold.

            The screener for this strategy synthesizes candidates locally
            from ``config.options.underlyings`` — no TradingView call. See
            ``intraday_tv_schwab_bot/_strategies/zero_dte_etf_options`` for
            a fully-fleshed working example.

            Public engine hooks implemented:
              * ``live_activity_score(frame)`` — returns a tape-aware
                multiplier (1.0 = neutral) the dashboard candidate ring
                renders. See _strategies/README.md "Extension hooks".
              * ``dashboard_directional_bias(frame)`` — returns
                Side.LONG / Side.SHORT / None for the candidate tile tone.

            Both must fail OPEN: return the neutral value when the frame
            is None/empty/insufficient. The engine has type guards that
            reject non-finite scores and non-Side bias results, but
            keeping the contract in the method itself keeps things
            predictable for subclasses.
            """

            strategy_name = {name!r}

            @staticmethod
            def live_activity_score(frame: pd.DataFrame | None) -> float:
                """Returns a tape-aware activity multiplier for the dashboard
                candidate ring. 1.0 = neutral pace; > 1.0 = elevated.
                Override with your own scoring math (e.g. volume momentum,
                ATR expansion). See zero_dte_etf_options for a 60/40
                volume-momentum + ATR-expansion blend."""
                if frame is None or frame.empty or len(frame) < 20:
                    return 1.0
                # TODO: implement your own activity-score math here.
                return 1.0

            def dashboard_directional_bias(self, frame: pd.DataFrame | None) -> Side | None:
                """Returns the underlying's current directional lean for
                the dashboard candidate tile tone. Conservative default
                requires VWAP-distance, EMA9-EMA20 gap, and day-return
                to all agree on one side; returns None otherwise. Reuses
                the strategy's own trend_vwap_distance_pct /
                trend_ema_gap_pct thresholds when present."""
                if frame is None or frame.empty or len(frame) < 5:
                    return None
                try:
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
                except Exception:
                    return None
                return None

            def entry_signals(
                self,
                candidates: list[Candidate],
                bars: dict[str, pd.DataFrame],
                positions: dict[str, Position],
                client=None,
                data=None,
            ) -> list[Signal]:
                """Option entry logic. Loop over candidates (which are
                underlyings, e.g. SPY/QQQ/IWM from options.underlyings),
                read bars[candidate.symbol] for the underlying's frame,
                gate on whatever regime logic you want, then hand each
                style to the shared entry stage as a PREMIUM proposal
                (``EntryProposal(..., direction=<the underlying's market
                direction>, stop=None, target=None, level_frame=None,
                gate_frame=sr_frame=<the underlying frame>)``) BEFORE the
                contract / chain work, and build the Signal with
                ``self.entry_policy.emit(admitted, ..., order_side=<the
                spread's order side>, premium_stop=<premium stop>,
                target=<premium target>, metadata=<option metadata:
                asset_type, strike, expiry, contract symbol, etc.>)``.

                See _strategies/zero_dte_etf_options/strategy.py for a
                fully-fleshed working example with regime confirmation,
                ORB/trend/credit styles, and option-chain selection.
                """
                self._reset_entry_decisions()
                out: list[Signal] = []
                min_bars = int(self.params.get("min_bars", 40) or 40)

                for candidate in candidates:
                    underlying = candidate.symbol
                    frame = bars.get(underlying)
                    if frame is None or len(frame) < min_bars:
                        self._record_entry_decision(
                            underlying,
                            "skipped",
                            [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)],
                        )
                        continue
                    # TODO: implement your option entry logic here.
                    # Suggested structure:
                    #   1. _regime_confirm(candidate, bars, data) ->
                    #      dict with ok, side, reasons, regime
                    #   2. select option contract from chain via the
                    #      Schwab client (delta target, OI/vol filters,
                    #      bid-ask spread)
                    #   3. build_single_option_order / build_vertical_order
                    #   4. self.entry_policy.admit(premium proposal) before
                    #      step 2, then self.entry_policy.emit(...) with the
                    #      full option metadata
                    self._record_entry_decision(underlying, "skipped", ["not_yet_implemented"])
                return out

            def should_force_flatten(self, position: Position) -> bool:
                # Option strategies typically have their own force-flatten
                # time via OptionsConfig.force_flatten_time. Use that here
                # if applicable; the scaffolded default just preserves
                # existing position state.
                return False
        '''
    ).lstrip()


def _strategy_py(name: str, class_stem: str, plugin_type: str) -> str:
    if plugin_type == "option":
        return _option_strategy_py(name, class_stem)
    return _stock_strategy_py(name, class_stem)


def _stock_screener_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        # SPDX-License-Identifier: MIT
        from ...models import Candidate, Side
        from ..screener_base import BaseStrategyScreener


        class {class_stem}Screener(BaseStrategyScreener):
            strategy_name = {name!r}

            def run(self) -> list[Candidate]:
                c = self._column
                min_rvol = float(self.config.active_strategy.params.get("min_rvol", 1.5) or 1.5)
                query = (
                    self._base_query()
                    .select(
                        "name",
                        "description",
                        "close",
                        "volume",
                        "market_cap_basic",
                        "relative_volume_10d_calc",
                        "change_from_open",
                    )
                    .where(
                        *self._liquid_equity_conditions(min_price=5.0),
                        c("relative_volume_10d_calc") >= min_rvol,
                    )
                )
                rows = self._execute(query)
                return self._candidate_rows(
                    rows,
                    strategy=self.strategy_name,
                    directional_bias_fn=lambda row: (
                        Side.LONG
                        if float(row.get("change_from_open", 0.0) or 0.0) > 0.30
                        else (Side.SHORT if float(row.get("change_from_open", 0.0) or 0.0) < -0.30 else None)
                    ),
                    activity_score_fn=lambda row: abs(float(row.get("change_from_open", 0.0) or 0.0)) * max(0.5, min(float(row.get("relative_volume_10d_calc", 0.0) or 0.0), 2.5)),
                )
        '''
    ).lstrip()


def _option_screener_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        # SPDX-License-Identifier: MIT
        from ...models import Candidate
        from ..screener_base import BaseStrategyScreener


        class {class_stem}Screener(BaseStrategyScreener):
            """Local-synthesis screener for the option strategy. Mirrors
            ``ZeroDteEtfOptionsScreener.run`` — see that docstring for the
            full rationale. Synthesizes candidates from
            ``config.options.underlyings`` without calling TradingView.
            Live activity_score + directional_bias are resolved at publish
            time by engine._publish_state via the strategy's
            ``live_activity_score`` and ``dashboard_directional_bias``
            public hooks (see _strategies/README.md "Extension hooks").
            """

            strategy_name = {name!r}

            def run(self) -> list[Candidate]:
                if not bool(self.config.options.enabled):
                    return []
                underlyings = list(self.config.options.underlyings)
                if not underlyings:
                    return []
                confirmation_map = dict(self.config.options.confirmation_symbols or {{}})
                out: list[Candidate] = []
                for rank, symbol in enumerate(underlyings, start=1):
                    sym = str(symbol or "").upper().strip()
                    if not sym:
                        continue
                    metadata = {{
                        "name": sym,
                        "confirm_index": confirmation_map.get(sym),
                    }}
                    out.append(Candidate(
                        symbol=sym,
                        strategy=self.strategy_name,
                        rank=rank,
                        activity_score=1.0,
                        directional_bias=None,
                        metadata=metadata,
                    ))
                return out
        '''
    ).lstrip()


def _screener_py(name: str, class_stem: str, plugin_type: str) -> str:
    if plugin_type == "option":
        return _option_screener_py(name, class_stem)
    return _stock_screener_py(name, class_stem)


def _strategy_block(name: str, plugin_type: str) -> dict[str, Any]:
    is_option = plugin_type == "option"
    params: dict[str, Any] = {"min_bars": 40}
    if not is_option:
        # Equity strategies parameterize symbols + rvol gate per-config.
        # Option strategies read these from the top-level options block,
        # not the per-strategy params block.
        params["symbols"] = ["SPY", "QQQ"]
        params["min_rvol"] = 1.5
    return {
        name: {
            "entry_windows": [["09:35", "14:00"]] if is_option else [["09:35", "11:30"]],
            "management_windows": [["09:30", "15:15"]] if is_option else [["09:30", "15:55"]],
            "screener_windows": [["09:35", "14:00"]] if is_option else [["09:35", "11:30"]],
            "params": params,
        }
    }


def _full_config_yaml(name: str, plugin_type: str) -> str:
    if not CANONICAL_TEMPLATE.exists():
        raise FileNotFoundError(f"canonical template not found: {CANONICAL_TEMPLATE}")
    template = yaml.safe_load(CANONICAL_TEMPLATE.read_text(encoding="utf-8")) or {}
    if not isinstance(template, dict):
        raise ValueError(f"canonical template must load to a mapping: {CANONICAL_TEMPLATE}")
    template["strategy"] = name
    template["strategies"] = _strategy_block(name, plugin_type)
    # Equity strategies don't read self.config.options.* anywhere — the
    # OptionsConfig dataclass defaults kick in if the block is absent. We
    # strip it from scaffolded stock-type yamls to match the rest of the
    # equity-strategy yamls (2026-05-19 cleanup removed the same block
    # from all 14 existing equity yamls). Option-type yamls keep the
    # block — option strategies actively read it.
    if plugin_type == "stock":
        template.pop("options", None)
    _reset_shared_knobs(template)
    header = (
        f"# Full runnable preset scaffolded from configs/config.example.yaml for {name}.\n"
        "# Its shared_entry / shared_exit sections, risk.time_stop_minutes and\n"
        "# support_resistance.entry_proximity_scoring_enabled are the config dataclass defaults.\n"
        "# Put SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_ACCOUNT_HASH, SCHWAB_ENCRYPTION_KEY, and TRADINGVIEW_SESSIONID in a .env file at the repo root (see .env.example).\n"
    )
    return header + yaml.safe_dump(template, sort_keys=False)


def _reset_shared_knobs(template: dict[str, Any]) -> None:
    """Give a scaffolded preset the dataclass defaults of the knobs
    config.example.yaml carries at peer_confirmed_htf_pivots parity.

    Since 2026-09-24 every preset states what its strategy effectively ran,
    and the example runs htf_pivots: several entry vetoes, the S/R proximity
    score, the time stop and the structure / chart / candle exits are off
    there, so a new plugin cloned from it silently took a peer strategy's
    choices. The shared_entry / shared_exit sections are replaced whole (a
    key their dataclass does not know would fail at load anyway); the two
    knobs that live in other sections are reset in place.
    """
    template["shared_entry"] = dataclasses.asdict(SharedEntryLogicConfig())
    template["shared_exit"] = dataclasses.asdict(SharedExitLogicConfig())
    template.setdefault("risk", {})["time_stop_minutes"] = RiskConfig().time_stop_minutes
    template.setdefault("support_resistance", {})["entry_proximity_scoring_enabled"] = (
        SupportResistanceConfig().entry_proximity_scoring_enabled
    )


def scaffold_plugin(name: str, class_stem: str | None, plugin_type: str, *, force: bool) -> Path:
    plugin_name = _snake_case(name)
    class_name = _camel_case(class_stem) if class_stem else _default_class_stem(plugin_name)
    target = STRATEGIES_DIR / plugin_name
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists; pass --force to overwrite")
    target.mkdir(parents=True, exist_ok=True)
    files = {
        # SPDX header + docstring matches the convention used by all 14
        # existing strategies' __init__.py files. Don't emit empty files.
        "__init__.py": '# SPDX-License-Identifier: MIT\n"""Strategy plugin package."""\n',
        "manifest.json": _manifest(plugin_name, class_name, plugin_type),
        "strategy.py": _strategy_py(plugin_name, class_name, plugin_type),
        "screener.py": _screener_py(plugin_name, class_name, plugin_type),
    }
    for filename, content in files.items():
        # encoding="utf-8" is required: the option-type strategy template
        # uses em-dashes in its docstrings. Without an explicit encoding,
        # Path.write_text falls back to the platform default (cp1252 on
        # Windows) and writes 0x97 instead of the UTF-8 em-dash sequence,
        # which then fails py_compile and the package import.
        (target / filename).write_text(content, encoding="utf-8")
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIGS_DIR / f"config.{plugin_name}.yaml").write_text(
        _full_config_yaml(plugin_name, plugin_type),
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a new strategy plugin scaffold.")
    parser.add_argument("name", help="Plugin directory / manifest name in snake_case or a human-readable name")
    parser.add_argument("--class-stem", help="Optional class stem; defaults to CamelCase(name), trimming a trailing '_strategy'")
    parser.add_argument("--plugin-type", choices=("stock", "option"), default="stock")
    parser.add_argument("--force", action="store_true", help="Overwrite files if the target already exists")
    args = parser.parse_args()

    target = scaffold_plugin(args.name, args.class_stem, args.plugin_type, force=args.force)
    print(target.relative_to(ROOT))
    print((CONFIGS_DIR / f"config.{_snake_case(args.name)}.yaml").relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
