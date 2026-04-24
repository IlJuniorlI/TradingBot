# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from textwrap import dedent
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "intraday_tv_schwab_bot" / "_strategies"
CONFIGS_DIR = ROOT / "configs"
CANONICAL_TEMPLATE = CONFIGS_DIR / "config.example.yaml"


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


def _manifest(name: str, class_stem: str, plugin_type: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "name": name,
            "type": plugin_type,
            "strategy_module": f"intraday_tv_schwab_bot._strategies.{name}.strategy",
            "strategy_class": f"{class_stem}Strategy",
            "screener_module": f"intraday_tv_schwab_bot._strategies.{name}.screener",
            "screener_class": f"{class_stem}Screener",
            "entry_windows": [["09:35", "11:30"]],
            "management_windows": [["09:30", "15:55"]],
            "screener_windows": [["09:35", "11:30"]],
            "params": {
                "symbols": ["SPY", "QQQ"],
                "min_bars": 40,
                "min_rvol": 1.5,
            },
            "capabilities": {
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
            },
        },
        indent=2,
    ) + "\n"


def _strategy_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        from ..shared import Candidate, Position, Signal, Side, pd
        from ..strategy_base import BaseStrategy


        class {class_stem}Strategy(BaseStrategy):
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
                allow_short = bool(getattr(self.config.risk, "allow_short", False))

                for candidate in candidates:
                    if candidate.symbol in positions:
                        self._record_entry_decision(candidate.symbol, "skipped", ["already_in_position"])
                        continue

                    frame = bars.get(candidate.symbol)
                    if frame is None or len(frame) < min_bars:
                        self._record_entry_decision(
                            candidate.symbol,
                            "skipped",
                            [self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)],
                        )
                        continue

                    last = frame.iloc[-1]
                    close = self._safe_float(last.get("close"), 0.0)
                    vwap = self._safe_float(last.get("vwap"), close)
                    rvol = self._safe_float(candidate.metadata.get("relative_volume_10d_calc"), 0.0)
                    day_strength = self._safe_float(candidate.metadata.get("change_from_open"), 0.0)

                    if rvol < min_rvol:
                        self._record_entry_decision(candidate.symbol, "skipped", ["rvol_too_low"])
                        continue

                    long_ok = close > vwap and day_strength > 0
                    short_ok = allow_short and close < vwap and day_strength < 0
                    if not long_ok and not short_ok:
                        self._record_entry_decision(candidate.symbol, "skipped", ["no_setup"])
                        continue

                    side = Side.LONG if long_ok else Side.SHORT
                    stop = close * (0.995 if side == Side.LONG else 1.005)
                    target = close * (1.010 if side == Side.LONG else 0.990)
                    setup_quality_score = 1.0
                    execution_quality_score = 0.0
                    activity_weight = 0.15
                    selection_quality_score = setup_quality_score + execution_quality_score
                    final_priority_score = selection_quality_score + (float(candidate.activity_score) * activity_weight)
                    signal = Signal(
                        symbol=candidate.symbol,
                        strategy=self.strategy_name,
                        side=side,
                        reason={name!r},
                        stop_price=stop,
                        target_price=target,
                        metadata={{
                            # entry_price is required by risk.py::_signal_entry_price —
                            # without it the same-level block + fib-pullback override
                            # short-circuit and never fire for this strategy.
                            "entry_price": close,
                            "rvol": rvol,
                            "day_strength": day_strength,
                            "activity_score": float(candidate.activity_score),
                            "setup_quality_score": setup_quality_score,
                            "execution_quality_score": execution_quality_score,
                            "final_priority_score": round(final_priority_score, 4),
                            "selection_quality_score": round(selection_quality_score, 4),
                            "trigger_score": 1.0,
                            "regime_score": 1.0,
                        }},
                    )
                    self._record_entry_decision(candidate.symbol, "signal", ["long_setup" if side == Side.LONG else "short_setup"])
                    out.append(signal)
                return out

            def should_force_flatten(self, position: Position) -> bool:
                return self._configurable_stock_force_flatten(position)
        '''
    ).lstrip()


def _screener_py(name: str, class_stem: str) -> str:
    return dedent(
        f'''
        from ..shared import Candidate, Side
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


def _strategy_block(name: str) -> dict[str, Any]:
    return {
        name: {
            "entry_windows": [["09:35", "11:30"]],
            "management_windows": [["09:30", "15:55"]],
            "screener_windows": [["09:35", "11:30"]],
            "params": {
                "symbols": ["SPY", "QQQ"],
                "min_bars": 40,
                "min_rvol": 1.5,
            },
        }
    }


def _full_config_yaml(name: str) -> str:
    if not CANONICAL_TEMPLATE.exists():
        raise FileNotFoundError(f"canonical template not found: {CANONICAL_TEMPLATE}")
    template = yaml.safe_load(CANONICAL_TEMPLATE.read_text()) or {}
    if not isinstance(template, dict):
        raise ValueError(f"canonical template must load to a mapping: {CANONICAL_TEMPLATE}")
    template["strategy"] = name
    template["strategies"] = _strategy_block(name)
    header = (
        f"# Full runnable preset scaffolded from configs/config.example.yaml for {name}.\n"
        "# Put SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_ACCOUNT_HASH, SCHWAB_ENCRYPTION_KEY, and TRADINGVIEW_SESSIONID in a .env file at the repo root (see .env.example).\n"
    )
    return header + yaml.safe_dump(template, sort_keys=False)


def scaffold_plugin(name: str, class_stem: str | None, plugin_type: str, *, force: bool) -> Path:
    plugin_name = _snake_case(name)
    class_name = _camel_case(class_stem) if class_stem else _default_class_stem(plugin_name)
    target = STRATEGIES_DIR / plugin_name
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists; pass --force to overwrite")
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "__init__.py": "",
        "manifest.json": _manifest(plugin_name, class_name, plugin_type),
        "strategy.py": _strategy_py(plugin_name, class_name),
        "screener.py": _screener_py(plugin_name, class_name),
    }
    for filename, content in files.items():
        (target / filename).write_text(content)
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIGS_DIR / f"config.{plugin_name}.yaml").write_text(_full_config_yaml(plugin_name))
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
