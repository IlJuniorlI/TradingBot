# Strategy Plugins

This directory uses a **manifest-first, directory-per-plugin** model.

## Current layout

The `_strategies/` directory now has:

- one **directory per strategy plugin**
- one `strategy.py` file inside each plugin directory
- one `screener.py` file inside each plugin directory
- one `manifest.json` file inside each plugin directory
- shared helper files directly in `_strategies/`
- no central registry file to edit by hand for each new strategy

A new strategy plugin is added by creating a directory like:

```text
intraday_tv_schwab_bot/_strategies/my_new_strategy/
  __init__.py
  strategy.py
  screener.py
  manifest.json
```

At startup, the bot reads only the `manifest.json` files to discover available strategies. It does **not** import every strategy module during discovery.

The actual Python modules are imported only when that strategy or screener is needed.

Runtime precedence is now: manifest/code defaults -> selected top-level config file -> CLI strategy override.

Shipped runtime presets live under `configs/config.<strategy>.yaml`.


## What lives where

- `_strategies/<name>/strategy.py` — strategy implementation
- `_strategies/<name>/screener.py` — screener implementation
- `_strategies/<name>/manifest.json` — lightweight manifest used for discovery and explicit plugin metadata
- `_strategies/strategy_base.py` — shared base class for strategy logic
- `_strategies/screener_base.py` — shared base class for screener logic
- `_strategies/plugin_api.py` — `StrategyManifest` dataclass
- `_strategies/registry.py` — manifest discovery and on-demand loading
- `_strategies/shared.py` — curated shared helpers/reexports for plugin files (import explicitly; do not use wildcard imports)

## Minimum requirements

A valid plugin must provide:

1. a strategy class that inherits `BaseStrategy` and sets `strategy_name`
2. a screener class that inherits `BaseStrategyScreener` and sets `strategy_name`

`strategy_name` should be a plain string that matches the plugin manifest `name`. New plugins should be fully self-contained and must not rely on any central strategy-name helper in `models.py`.

Optional manifest capabilities and strategy hooks
- `manifest.json -> capabilities.dashboard.tradable_symbols_source` can declare the dashboard/watchlist universe without adding engine branches. Supported values: `params.tradable`, `params.symbols`, `options.underlyings`, `pairs.symbols`, `none`.
- `manifest.json -> capabilities.dashboard.candidate_limit_mode` can declare how many candidates the dashboard should show. Supported values: `default`, `tradable_count`, `fixed` (with `capabilities.dashboard.candidate_limit`).
- `manifest.json -> schema_version` is now part of the plugin contract. Current supported value: `1`.
- `manifest.json -> capabilities.dashboard.allow_generic_level_fallback` can opt a plugin into the generic dashboard HTF level fallback behavior.
- `manifest.json -> capabilities.dashboard.level_context` can declaratively override the generic dashboard HTF/trigger context parameters used for strategy watchlist symbol cards.
- `manifest.json -> capabilities.dashboard.candidate_labels` can map dashboard overlay kinds like `bullish_continuation_trigger` or `prior_day_low` to label pills without overriding Python code.
- `manifest.json -> capabilities.dashboard.candidate_sources` can map dashboard overlay kinds to one or more source tags for watchlist card zones.
- `manifest.json -> capabilities.dashboard.zone_width` can declaratively control watchlist card zone width with `fixed`, `atr_mult`, `pct_of_price`, or `max_of` policies, plus optional `kind_overrides`.
- `manifest.json -> capabilities.startup_restore.eligible_symbols_source` can declare the stock startup-restore universe. Supported values: `dashboard_tradable_symbols`, `params.tradable`, `params.symbols`, `options.underlyings`, `pairs.symbols`, `all`, `none`.
- `manifest.json -> capabilities.startup_restore.require_hybrid_metadata` can force hybrid startup restore to require stored metadata for strategies that need it.
- `manifest.json -> capabilities.signal_priority.metadata_fields` can declare metadata-driven signal ranking order without overriding Python code.
- `manifest.json -> capabilities.watchlist.active_sources` can declaratively build the streaming/history watchlist from standard symbol sources such as `candidates`, `positions.underlyings_or_symbols`, `positions.reference_symbols`, `dashboard_tradable_symbols`, `params.peers`, `pairs.symbols`, `pairs.references`, `options.underlyings`, and `options.confirmation_symbols`.
- `manifest.json -> capabilities.watchlist.quote_sources` can declaratively build the quote watchlist from standard symbol sources such as `active_watchlist`, `options.volatility_symbol`, `options.confirmation_symbols`, or filtered position metadata descriptors like `positions.metadata_list` for option valuation legs.
- `@classmethod normalize_params(cls, params)` lets a plugin normalize its own manifest/config params without adding strategy-name branches to the generic config loader.
- `strategy_logic_default(self, section, key, default)` lets a plugin override shared logic defaults without adding strategy-name branches to `BaseStrategy`.
- `manifest.json -> capabilities.history.required_bars` can set a fixed startup warmup bar requirement for simple strategies that do not need a custom formula.
- `required_history_bars(self, symbol=None, positions=None)` still exists for strategies that need a formula based on params or position state.
- `signal_priority_key(...)` and the other runtime hooks still exist as the escape hatch for behavior that is too custom to express cleanly in the manifest.
- `dashboard_level_context_spec()`, `dashboard_candidate_label()`, and `dashboard_candidate_sources()` let a strategy customize generic dashboard level/zones rendering without adding engine strategy-name branches.
- `risk.trade_management_mode: adaptive_ladder` is now **universally supported** at the `BaseStrategy` level. The default `_build_ladder_rungs(side, close, stop, atr, sr_ctx, regime=...)` walks `sr_ctx.resistances` (long) or `sr_ctx.supports` (short), filtering by `ladder_min_target_rr` (default 1.2) and capped at `ladder_max_rungs` (default 4). Strategies that need custom rung logic override the method (e.g. `peer_confirmed_key_levels` uses HTF peer-confirmed levels; `top_tier_adaptive` returns `[]` for range regime so single-target behavior is preserved). Strategies that should never run ladder mode set the class attribute `supports_adaptive_ladder = False` — when the config requests `adaptive_ladder` against such a strategy, the engine logs a startup WARNING and falls back to trailing-stop behavior.

### Canonical runtime contract for plugins

New plugins should use the standardized runtime field names below.

- Candidate objects:
  - `activity_score` — how “in play” the symbol is before deeper validation.
  - `directional_bias` — optional long/short hint from the screener.
- Signal metadata:
  - `final_priority_score` — the main ranking score used by the engine.
  - `selection_quality_score` — tie-break score when two signals have similar final priority.
  - `activity_score` — copied through from the candidate when useful for dashboards/logs.
  - `setup_quality_score` / `execution_quality_score` — optional decomposition fields for cleaner plugin design.
  - `trigger_score`, `regime_score`, `directional_peer_score`, `peer_score`, `directional_vote_edge`, `runner_quality_score`, `execution_headroom_score`, `source_quality_score` — optional standardized ranking components. Prefer `directional_peer_score` for final ranking whenever a raw peer score must be interpreted differently for longs vs shorts.

The old `score`, `side_bias`, `signal_strength`, and `signal_priority_tiebreak` names are no longer part of the plugin contract.

Example capability block:

```json
"capabilities": {
  "dashboard": {
    "tradable_symbols_source": "params.tradable",
    "candidate_limit_mode": "tradable_count",
    "allow_generic_level_fallback": true
  },
  "startup_restore": {
    "eligible_symbols_source": "dashboard_tradable_symbols",
    "require_hybrid_metadata": false
  },
  "signal_priority": {
    "metadata_fields": [
      "trigger_score",
      "regime_score",
      "directional_peer_score",
      "selection_quality_score"
    ]
  },
  "watchlist": {
    "active_sources": [
      "candidates",
      "positions.underlyings_or_symbols",
      "positions.reference_symbols",
      "dashboard_tradable_symbols"
    ],
    "quote_sources": [
      "active_watchlist"
    ]
  },
  "history": {
    "required_bars": 40
  }
}
```
3. a `manifest.json` file that explicitly names both classes and modules
5. a plugin directory name that exactly matches the strategy `name`

There is **no central registry** to edit.

## Quick add-a-strategy checklist

1. Create `_strategies/<name>/`
2. Create `_strategies/<name>/__init__.py`
3. Create `_strategies/<name>/strategy.py`
4. Create `_strategies/<name>/screener.py`
5. Create `_strategies/<name>/manifest.json`
6. Create `configs/config.<name>.yaml` if you want to ship a tuned runtime preset for the new plugin
7. Put the strategy class in `strategy.py`
8. Put the screener class in `screener.py`
9. Make both classes inherit the correct base class
10. Make the manifest `name` unique and lowercase
11. Make the plugin directory name match the manifest `name` exactly
12. Add default params and windows to the manifest
13. Set `strategy: <name>` in YAML to use it
14. Compile and smoke-test the plugin before production use

A scaffold generator is included now:

```bash
python scripts/scaffold_strategy_plugin.py my_new_strategy
```

That command creates a new plugin directory with `manifest.json`, `strategy.py`, `screener.py`, and `__init__.py`, and also writes a matching top-level runtime preset at `configs/config.<strategy>.yaml`.

## Minimal stock strategy example

Create:

```text
intraday_tv_schwab_bot/_strategies/my_new_strategy/
  __init__.py
  strategy.py
  screener.py
  manifest.json
```

### Example `strategy.py`

Avoid `from ..shared import *`. Import only the names your plugin uses.


```python
from ..shared import (
    Candidate,
    Position,
    Signal,
    Side,
    pd,
)
from ..strategy_base import BaseStrategy


class MyNewStrategy(BaseStrategy):
    strategy_name = "my_new_strategy"

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

        min_bars = int(self.params.get("min_bars", 40))
        min_rvol = float(self.params.get("min_rvol", 1.5))
        allow_short = bool(self.config.risk.allow_short)

        for c in candidates:
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue

            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(
                    c.symbol,
                    "skipped",
                    [self.insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)],
                )
                continue

            last = frame.iloc[-1]
            close = self._safe_float(last.get("close"), 0.0)
            vwap = self._safe_float(last.get("vwap"), close)
            day_strength = self._safe_float(c.metadata.get("change_from_open"), 0.0)
            rvol = self._safe_float(c.metadata.get("relative_volume_10d_calc"), 0.0)

            if rvol < min_rvol:
                self._record_entry_decision(c.symbol, "skipped", ["rvol_too_low"])
                continue

            long_ok = close > vwap and day_strength > 0
            short_ok = allow_short and close < vwap and day_strength < 0

            if not long_ok and not short_ok:
                self._record_entry_decision(c.symbol, "skipped", ["no_setup"])
                continue

            side = Side.LONG if long_ok else Side.SHORT
            stop = close * (0.995 if side == Side.LONG else 1.005)
            target = close * (1.010 if side == Side.LONG else 0.990)

            signal = Signal(
                symbol=c.symbol,
                strategy=self.strategy_name,
                side=side,
                reason="my_new_strategy",
                stop_price=stop,
                target_price=target,
                metadata={
                    "entry_price": close,  # required by risk.py::_signal_entry_price for same-level block + fib override
                    "rvol": rvol,
                    "day_strength": day_strength,
                },
            )
            self._record_entry_decision(c.symbol, "signal", ["long_setup" if side == Side.LONG else "short_setup"])
            out.append(signal)

        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
```

### Example `screener.py`

```python
from ..shared import Candidate, Side
from ..screener_base import BaseStrategyScreener


class MyNewStrategyScreener(BaseStrategyScreener):
    strategy_name = "my_new_strategy"

    def run(self) -> list[Candidate]:
        c = self._column
        params = self.config.active_strategy.params
        min_rvol = float(params.get("min_rvol", 1.5))

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
            .order_by("change_from_open", ascending=False)
        )

        df = self._execute(query)
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: (
                Side.LONG
                if float(row.get("change_from_open", 0.0) or 0.0) > 0.30
                else (Side.SHORT if float(row.get("change_from_open", 0.0) or 0.0) < -0.30 else None)
            ),
            activity_score_fn=lambda row: abs(float(row.get("change_from_open", 0.0) or 0.0)) * max(0.5, min(float(row.get("relative_volume_10d_calc", 0.0) or 0.0), 2.5)),
        )
```

### Example `manifest.json`

```json
{
  "schema_version": 1,
  "name": "my_new_strategy",
  "type": "stock",
  "strategy_module": "intraday_tv_schwab_bot._strategies.my_new_strategy.strategy",
  "strategy_class": "MyNewStrategy",
  "screener_module": "intraday_tv_schwab_bot._strategies.my_new_strategy.screener",
  "screener_class": "MyNewStrategyScreener",
  "entry_windows": [["09:45", "15:30"]],
  "management_windows": [["09:35", "15:55"]],
  "screener_windows": [["09:35", "15:30"]],
  "params": {
    "min_bars": 40,
    "min_rvol": 1.5,
    "force_flatten": {"long": true, "short": true}
  }
}
```

## Required manifest fields

- `name` — unique strategy name string and directory name
- `type` — either `"stock"` or `"option"`
- `strategy_module` — import path for `strategy.py`
- `strategy_class` — class name exported by `strategy.py`
- `screener_module` — import path for `screener.py`
- `screener_class` — class name exported by `screener.py`
- `entry_windows` — list of `[start, end]` `HH:MM` windows
- `management_windows` — list of `[start, end]` `HH:MM` windows
- `screener_windows` — list of `[start, end]` `HH:MM` windows
- `params` — default strategy params object

## Important rules

### 1. Directory name and manifest name must match

These must line up exactly:

- plugin directory: `my_new_strategy/`
- manifest field: `"name": "my_new_strategy"`

### 2. Use a unique lowercase name

Use a simple lowercase strategy name like:

- `my_new_strategy`
- `volatility_squeeze_breakout`
- `sector_rotation_pullback`

Do not reuse an existing plugin name.

### 3. Keep one plugin package per strategy

The intended pattern is **one plugin directory per strategy**.

That directory should contain:

- `__init__.py`
- `strategy.py`
- `screener.py`
- `manifest.json`
- any extra plugin-specific support files you later decide to add

### 4. Keep import-time side effects out of the modules

Because the selected plugin modules are imported only on demand, each plugin file should stay lightweight at module import time.

Do **not** do things like:

- network requests at import time
- file I/O at import time
- heavy calculations at import time
- constructing big cached datasets at import time

Define classes and helpers only.

## How config picks it up

When you add a new plugin directory, the bot will:

1. discover your `manifest.json` automatically
2. validate the manifest fields
3. load the default windows/params into config
4. import `strategy.py` only when building the strategy instance
5. import `screener.py` only when building the screener instance

No registry edit is required.

The loader no longer guesses module paths. Every manifest must explicitly declare `strategy_module` and `screener_module`.

## Validation rules enforced by the loader

The loader enforces that:

- manifest `name` is non-empty
- plugin directory name matches manifest `name`
- `strategy_module` is explicitly declared
- `screener_module` is explicitly declared
- `strategy_module` exists and imports
- `screener_module` exists and imports
- `strategy_class` exists in `strategy.py`
- `screener_class` exists in `screener.py`
- `strategy_class` inherits `BaseStrategy`
- `screener_class` inherits `BaseStrategyScreener`
- `type` is explicitly declared
- `type` is `stock` or `option`
- window fields are lists of `[start, end]`
- `params` is a JSON object

## Common failure cases

Typical plugin mistakes are:


- directory name does not match manifest `name`
- duplicate strategy `name`
- missing `manifest.json`
- missing `strategy.py`
- missing `screener.py`
- missing `strategy_module`
- missing `screener_module`
- missing `type`
- typo in `strategy_module`
- typo in `screener_module`
- typo in `strategy_class`
- typo in `screener_class`
- invalid window format
- invalid `type` value
- invalid `params` object

## Troubleshooting

### Strategy is discovered but fails to build

Usually one of these is wrong:

- `strategy_module` path is wrong
- `strategy_class` name is wrong
- the class does not inherit `BaseStrategy`
- `strategy.py` has an import-time error

### Screener is discovered but fails to build

Usually one of these is wrong:

- `screener_module` path is wrong
- `screener_class` name is wrong
- the class does not inherit `BaseStrategyScreener`
- `screener.py` has an import-time error
- TradingView screener dependencies are not installed

### Screener works but strategy params are missing

That usually means:

- the YAML override is under the wrong strategy name
- manifest `params` missing a default you expected
- the strategy is reading the wrong param key

## Style guidance

Recommended practices:

- keep one strategy class per `strategy.py`
- keep one screener class per `screener.py`
- keep manifests small, declarative, and explicit
- prefer additive params with safe defaults
- use `self.config.active_strategy.params` or `self.params` consistently
- avoid hidden import-time work
- keep module names stable once released
