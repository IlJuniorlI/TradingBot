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
- `_strategies/shared_entry.py` — the shared entry stage (`SharedEntryPolicy`: `admit` / `emit` / `rank_key`, `EntryProposal`, divergence entries)
- `_strategies/shared_exit.py` — the shared exit policy (`SharedExitPolicy`, `ExitTape`, `EXIT_FAMILY_GATES`, `bar_closed_after`)
- `_strategies/shared.py` — curated shared helpers/reexports for plugin files (import explicitly; do not use wildcard imports)

## Minimum requirements

A valid plugin must provide:

1. a strategy class that inherits `BaseStrategy` and sets `strategy_name`
2. a screener class that inherits `BaseStrategyScreener` and sets `strategy_name`
3. a `manifest.json` file that explicitly names both classes and modules
4. a plugin directory name that exactly matches the strategy `name`

`strategy_name` should be a plain string that matches the plugin manifest `name`. New plugins should be fully self-contained and must not rely on any central strategy-name helper in `models.py`.

Every entry goes through the shared entry stage and every exit through the shared exit policy; see [The shared entry and exit contract](#the-shared-entry-and-exit-contract-2026-09-24) below. A strategy that builds a `Signal` itself, reads `config.shared_entry` / `config.shared_exit`, or overrides a shared hook fails the contract test, or raises at import.

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
- `manifest.json -> capabilities.signal_priority` declares how the gatekeeper ranks the strategy's signals (`primary_field`, `shared_score_weight`, `rank_unit_field`, `metadata_fields`); see the contract below. It is the only way to change the ranking: the `signal_priority_key(...)` override is gone.
- `manifest.json -> capabilities.shared_entry` exempts a style from named shared vetoes (`exemptions`) or opts the strategy out of divergence-only entries (`divergence_entry: false`); see the contract below.
- `manifest.json -> capabilities.watchlist.active_sources` can declaratively build the streaming/history watchlist from standard symbol sources such as `candidates`, `positions.underlyings_or_symbols`, `positions.reference_symbols`, `dashboard_tradable_symbols`, `params.peers`, `pairs.symbols`, `pairs.references`, `options.underlyings`, and `options.confirmation_symbols`.
- `manifest.json -> capabilities.watchlist.quote_sources` can declaratively build the quote watchlist from standard symbol sources such as `active_watchlist`, `options.volatility_symbol`, `options.confirmation_symbols`, or filtered position metadata descriptors like `positions.metadata_list` for option valuation legs.
- `@classmethod normalize_params(cls, params)` lets a plugin normalize its own manifest/config params without adding strategy-name branches to the generic config loader.
- **Reserved names.** `BaseStrategy.__init_subclass__` raises `TypeError` when a strategy class defines `position_exit_signal`, `shared_exit_signal`, `strategy_logic_default` or `signal_priority_key` (2026-09-24). A knob is set in the preset YAML, not rewritten by the strategy. A style is exempted from a veto in the manifest, a strategy's own exits go in `strategy_exit_signal`, and ranking is declared in `capabilities.signal_priority`. An out-of-tree plugin therefore cannot quietly opt out of the global knobs.
- **Exits.** The shared exit families (time stop, chart / candle pattern, CHoCH and bias structure, technical, S/R loss, divergence scale-out) are decided for every strategy by `shared_exit.SharedExitPolicy`, which the position manager owns. A strategy's OWN exits go in `strategy_exit_signal(self, position, bars, tape, data=None) -> ExitDecision | None`, which runs after every shared family held and holds by default. `tape` is the `ExitTape` the shared families read (close, EMA9 / EMA20 / VWAP, close position; None where a value is missing), so a hook judges the same references. Return `ExitDecision(reason, "strategy")` for a full exit. `peer_confirmed_key_levels` (and its subclasses) implements its adaptive-ladder defence there, and `microcap_pm_breakout` its blowoff guard. The exit graces key on `metadata['entry_style_family']` (`orb`, `pullback`), which `emit` stamps from the proposal's style family. A hook that judges a structure event or pivot against the entry must compare the bar's CLOSE, not its label: use `shared_exit.bar_closed_after(label, position.entry_time, bar_minutes)`. Bars are labelled at their start, so an event on the bar the entry filled in is post-entry. The peer ladder does this for its HTF CHoCH / BoS.
- `manifest.json -> capabilities.history.required_bars` can set a fixed startup warmup bar requirement for simple strategies that do not need a custom formula.
- `required_history_bars(self, symbol=None, positions=None)` still exists for strategies that need a formula based on params or position state.
- The other runtime hooks still exist as the escape hatch for behavior that is too custom to express cleanly in the manifest.
- `dashboard_level_context_spec()`, `dashboard_candidate_label()`, and `dashboard_candidate_sources()` let a strategy customize generic dashboard level/zones rendering without adding engine strategy-name branches.
- `live_activity_score(frame)`, `dashboard_directional_bias(frame)`, and `dashboard_change_from_open(frame)` are **optional public hooks** for strategies whose screeners can't populate real `Candidate.activity_score` / `Candidate.directional_bias` / `Candidate.metadata["change_from_open"]` at screen time (e.g. local-synthesis screeners that don't call TradingView). When a strategy defines them, `engine._publish_state` resolves the values against the streamed bars frame at publish time and overrides the candidate stubs in the dashboard payload. Strategies that don't define them are unaffected — the candidate's existing values flow through. Dispatch is pure duck-typing via `getattr`; no plugin-type branches in the engine. `live_activity_score` should return a float (1.0 = neutral pace; the strategy's own `min_activity_for_entry` / `trend_activity_threshold` / `credit_activity_min` / `credit_activity_max` knobs gate against it). `dashboard_directional_bias` should return `Side.LONG` / `Side.SHORT` / `None`. `dashboard_change_from_open` should return a float in PERCENT units (e.g. `1.23` for +1.23%, matching the unit produced by TradingView's `change_from_open` field and the Schwab quote's `percent_change`), or `None` when the frame is insufficient. All three must fail open (return the neutral value) when the frame is None/empty/insufficient — non-finite scores, non-`Side` bias results, and non-finite change values are rejected by engine-side type guards. All three resolvers share a single `data.get_merged` frame fetch per candidate (cycle cache means no API call).
- `risk.trade_management_mode: adaptive_ladder` is now **universally supported** at the `BaseStrategy` level. The default `_build_ladder_rungs(side, close, stop, atr, sr_ctx, regime=...)` walks `sr_ctx.resistances` (long) or `sr_ctx.supports` (short), filtering by `ladder_min_target_rr` (default 1.2) and capped at `ladder_max_rungs` (default 4). Strategies that need custom rung logic override the method (e.g. `peer_confirmed_key_levels` uses HTF peer-confirmed levels; `top_tier_adaptive` returns `[]` for range regime so single-target behavior is preserved). Strategies that should never run ladder mode set the class attribute `supports_adaptive_ladder = False` — when the config requests `adaptive_ladder` against such a strategy, the engine logs a startup WARNING and falls back to trailing-stop behavior.

- **Divergence entries and exits (opt-in, run centrally).** Nothing to call. The shared entry stage computes each symbol's divergence entry candidates once per cycle, one per side, when `shared_entry.use_divergence_entry_signal` is on (off in every preset). A candidate on a proposal's own side adds `divergence_entry_score_bump` to its shared score, and one only on the opposite side is stamped as a conflict. After `entry_signals`, the gatekeeper asks the stage for divergence-only entries on the symbols the strategy produced no signal for. They go through the same `admit` / `emit` and always rank behind the strategy's own signals. The root README's `shared_entry` section has the candidate rules (current-session pivot, age window, S/R confluence, score, stop and target). A strategy opts out with `capabilities.shared_entry.divergence_entry: false` (`pairs_residual` and both 0DTE strategies do). The divergence EXIT is the last stage of `shared_exit.SharedExitPolicy` when `shared_exit.use_divergence_exit_signal` is on (off in every preset). A counter-direction REGULAR divergence (RSI, then OBV) forming on the held position, with its latest pivot's bar closed after the entry, scales the position out by `divergence_exit_partial_frac`, once per pivot. "Counter" is against the trade's market direction, so a bull put spread watches for a bearish one. The scale-out is an `ExitDecision` with `fraction < 1`, which the position manager sizes with a floor and sends at that quantity. Hidden divergence is continuation context and is never an exit trigger. Detection itself runs inside `build_technical_levels_context` (LTF) and `build_htf_context` (HTF, RSI only, built centrally by the data feed from `technical_levels.enabled` / `divergence_enabled` / `htf_divergence_max_age_bars`).

- **HTF context for the score terms.** The HTF divergence score term (`shared_entry.use_htf_divergence_score`) reads `EntryProposal.htf_ctx`. A strategy that already builds its own HTF context passes it there; the peer strategies pass their 60m contexts. Otherwise `admit` reads `_default_htf_context_for_score(symbol, data)`, a lightweight build of the strategy's own HTF frame (`htf_minutes` / `htf_lookback_days`, the one the engine refreshes) with the `support_resistance` level settings and the strategy's HTF EMA spans. It never refreshes, and it returns `None` on failure, which scores 0. It is only read while the knob is on and the proposal brings no context.

### Canonical runtime contract for plugins

New plugins should use the standardized runtime field names below.

- Candidate objects:
  - `activity_score` — how “in play” the symbol is before deeper validation.
  - `directional_bias` — optional long/short hint from the screener.
- Signal metadata:
  - `final_priority_score` — the default ranking score. `emit` computes it as `strategy_priority_score` + `shared_context_score`; a strategy passes only its own `strategy_score`.
  - `strategy_priority_score` / `shared_context_score` — the strategy's own priority and the shared entry-context + FVG (+ divergence bump) score, both stamped by `emit`.
  - `entry_style_family` / `regime` — the proposal's style family (the exit graces key on it) and its style (the session report groups by it), stamped by `emit`.
  - `selection_quality_score` — tie-break score when two signals have similar final priority.
  - `activity_score` — copied through from the candidate when useful for dashboards/logs.
  - `setup_quality_score` / `execution_quality_score` — optional decomposition fields for cleaner plugin design.
  - `ltf_score`, `regime_score`, `directional_peer_score`, `peer_score`, `directional_vote_edge`, `runner_quality_score`, `execution_headroom_score`, `source_quality_score` — optional standardized ranking components. Prefer `directional_peer_score` for final ranking whenever a raw peer score must be interpreted differently for longs vs shorts.

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
      "ltf_score",
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

There is **no central registry** to edit.

## The shared entry and exit contract (2026-09-24)

Every `shared_entry` and `shared_exit` knob is global: it acts on every strategy whose preset sets it, and the strategy does not call anything to get it. A strategy keeps its own setup logic, alternatives and selection. The shared stage owns everything the knobs control: the vetoes, the stop / target refinement, the FVG retest, the score terms, the divergence triggers, the exit families, the ranking and the signal itself. Until 2026-09-24 each strategy called the knob helpers it chose to, so most knobs did nothing for most strategies, and a `strategy_logic_default` hook let a strategy rewrite any knob. The rules below keep that from coming back.

### Entries: propose, admit, emit

For each alternative its own logic produces (a side, a regime, a style), the strategy builds an `EntryProposal` and hands it to `self.entry_policy.admit(proposal)`. `self.entry_policy` is the strategy's `SharedEntryPolicy` (`_strategies/shared_entry.py`), built by `BaseStrategy.__init__`.

```python
from ..shared_entry import EntryContexts, EntryProposal, RetestTrigger
```

`EntryProposal` fields:

- `candidate`: the screener candidate. `direction`: the MARKET direction; for an option it is the underlying's, so a bull put credit spread is sold but is a LONG proposal.
- `style`: the regime / style token. It is the manifest exemption key, the `regime` stamp and, unless `failure_key` is set, the key a refusal is recorded under.
- `style_family`: one of `STYLE_FAMILIES` (`trend`, `pullback`, `momentum`, `breakout`, `orb`, `range`, `sr_scalp`, `vol_squeeze`, `vwap_reclaim`, `reversal`, `pairs`, `peer`, `pivot`, `continuation`, `divergence`, `option_debit`, `option_credit`, `option_long`). It is stamped as `entry_style_family`, and the exit graces key on it: `orb` gets the ORB grace, `pullback` the longer pullback grace. Choose it by what the entry IS, not by strategy.
- `close`, `stop`, `target`: price levels. `target` None is a runner. A premium (option) proposal carries no price stop or target.
- Frames: `gate_frame` (structure, technical, chart and candle contexts and their vetoes), `sr_frame` (the S/R context, the broken-level ATR, the divergence candidates), `level_frame` (the refinement clamp's ATR; None marks a premium proposal, which skips the level stage), and `zone_frame` (the FVG / order-block retest plans and the FVG score term; None = the gate frame). Pin each read to the frame the strategy's own logic uses.
- `pending_reasons`: the strategy's own blockers. They refuse the proposal unless the retest admission clears them. Include `self._entry_exhaustion_reasons(...)` here when the strategy uses the anti-chase checks.
- `deferrable` and `retest`: the reason prefixes a retest may clear, and a `RetestTrigger(trigger_level, breakout_active, vwap, ema9, zones=("fvg",) or ("fvg", "ob"), anchor_stop=True)`. Clearing needs every pending reason to be deferrable. With `anchor_stop`, an admitting plan pulls the stop to its zone, bounded by `min_stop_atr_mult`.
- `stop_resolver`: computes the stop from the retest plans (microcap_pm_breakout's PMH stop). Returning None refuses the proposal as `stop_above_entry`.
- `raw_rr_gate`: a failure label. With it, the raw stop / target must clear `min_target_rr` before anything else runs.
- `htf_ctx`: the HTF context the HTF divergence score term reads (None = the default build).
- `contexts`: `EntryContexts(sr=, ms=, tech=, chart=)`, contexts the strategy already built on the declared frames, so `admit` does not build them twice.
- `vol_scale`: scales the broken-level guard's percent clearance.
- `failure_details`: gate snapshots / near-miss data recorded with a refusal.
- `symbol`: the traded symbol when it is not the candidate's (a pairs leg).
- `failure_key`: the refusal key when the strategy's loop consumes failures under another token than the style.

Construction validates the proposal. A deferrable set without a retest, a premium proposal with a price level, or a price proposal with neither a stop nor a resolver raises.

`admit` runs the raw R:R gate, the contexts, the retest admission, every switched-on veto (minus the manifest's exemptions for this style), the refinement and the score. The root README's `shared_entry` section has the order and every knob.

- **On refusal** it returns None and records the refusal once with `_set_build_failure(symbol, failure_key or style, primary, reasons=every blocker, details=failure_details)`. Consume it the way your loop already does: `self._consume_build_failure_payload(symbol, key)["reasons"]` into `self._record_entry_decision(symbol, "skipped", reasons)`, so the decision log lists every blocker.
- **On admission** it returns an `AdmittedEntry`. It is frozen, and only `admit` can mint one (constructing it or `dataclasses.replace` raises). It carries the refined `stop` / `target`; the `sr` / `ms` / `tech` / `chart` / `htf` contexts; `candle_signal`; `retest_plans`; `admitted_via_retest`; `fvg` (including `fvg_continuation_bias` / `fvg_reversal_bias` for the runner and management); `adjustments`; `shared_context_score`; `divergence_confirmation`; `gates_applied`; and `gates_exempted`. Everything after admission reads the ADMITTED levels: the ladder, the runner, `_adaptive_management_components`, R:R metadata and tier labels.

The signal is built with `self.entry_policy.emit(...)`:

```python
signal = self.entry_policy.emit(
    admitted,
    reason=reason,
    strategy_score=strategy_score,   # the strategy's own priority, WITHOUT the shared terms
    management=management,           # _adaptive_management_components(...) ({} allowed)
    target=admitted.target,          # or the ladder's active rung, or None (runner)
    ladder_meta=ladder_meta,         # optional
    metadata={...},                  # the strategy's own keys
    # options / pairs only: order_side=, premium_stop=, reference_symbol=, pair_id=
)
```

- `emit` stamps `final_priority_score = strategy_score + shared_context_score`, plus `strategy_priority_score`, `shared_context_score`, `entry_style_family`, `regime` (defaults to the style), `orb_window_entry`, `entry_price` (price-level signals), the `shared_entry_*` stamps and the shared context lists. They are merged over the strategy's own metadata. If a score key of your own should include the shared terms, compute it from `strategy_score + admitted.shared_context_score`.
- A target other than `admitted.target`, the ladder's active rung or None raises.
- For an option: pass the order side as `order_side` when it differs from the direction, and the premium stop as `premium_stop` (required for premium proposals, refused otherwise). `metadata['direction']` (`bullish*` / `bearish*`) must agree with the proposal's direction, or `emit` raises.

Worked examples, simplest first:

- `mean_reversion` / `closing_reversal`: pending reasons, one proposal.
- `momentum_close`, `opening_range_breakout`, `rth_trend_pullback`, `volatility_squeeze_breakout`: exhaustion reasons plus an FVG retest.
- `top_tier_adaptive`: `raw_rr_gate`, prebuilt contexts, `vol_scale`, ladder after admission.
- `peer_confirmed_key_levels`: `zone_frame`, `failure_key`, ladder re-qualified from the refined stop.
- `microcap_pm_breakout`: `stop_resolver`, `zones=("fvg", "ob")`, `anchor_stop=False`.
- `pairs_residual`: `symbol`, `reference_symbol`, `pair_id`.
- `zero_dte_etf_options`: premium proposals, `order_side`, `premium_stop`.

### What a strategy may not do

`tests/test_shared_knob_contract.py` checks every module, and a violation names its file and line:

- (a) Only `shared_entry.py` and `shared_exit.py` reference `config.shared_entry` / `config.shared_exit`, including through `getattr` / `hasattr` / `setattr`.
- (b) Only `shared_entry.py` constructs `Signal(...)` or `AdmittedEntry(...)`, also under an import alias.
- (c) A `strategy.py` may not define, call or import a helper that moved into the policy (the knob accessors, the veto predicates, the refinement passes, the retest plans, the score terms, the divergence candidate, `_build_signal_metadata`; the full list is `MOVED_HELPERS` in the test). From `shared_entry` it may import only `EntryProposal`, `EntryContexts`, `RetestTrigger`, `AdmittedEntry`, `STYLE_FAMILIES`, `VETO_GATES` and `DIVERGENCE_ENTRY_SOURCE`.
- (d) `BaseStrategy.__init_subclass__` refuses `position_exit_signal`, `shared_exit_signal`, `strategy_logic_default` and `signal_priority_key`, at import.
- (e) A `strategy.py` may not rewrite what `emit` built: no `dataclasses.replace` under any alias (including the `replace` re-exported by `_strategies/shared.py`), and no assignment or `setattr` of `.stop_price` / `.target_price`. It may not reach into the policy's privates (`self.entry_policy._x`, directly, through an alias or through `getattr`). Pass the stop / target through the proposal and the ladder, and metadata through `emit(metadata=...)`.

### Manifest capabilities

```json
"capabilities": {
  "shared_entry": {
    "exemptions": {"<style>": ["structure", "sr"]},
    "divergence_entry": false
  },
  "signal_priority": {
    "primary_field": "regime_score_normalized",
    "shared_score_weight": 1.0,
    "rank_unit_field": "regime_rank_unit",
    "metadata_fields": ["ltf_score", "selection_quality_score"]
  }
}
```

The `signal_priority` block is top_tier's declaration plus a metadata tail. Every field it names should be metadata the strategy stamps: a missing one ranks as 0, and a missing unit field zeroes the shared term. `rank_unit_field` is only valid with a positive `shared_score_weight`. A strategy whose primary is `final_priority_score` (which already contains the shared terms) keeps the default weight 0 and declares no unit field.

- `shared_entry.exemptions` maps a proposal `style` to the vetoes it skips. The gates come from `VETO_GATES` (`structure`, `sr`, `broken_level`, `chart`, `dual_divergence`, `candle`); a list needs at least one gate and no repeats. `divergence_entry` defaults to true. Unknown keys fail at load, and a knob cannot be set here.
- `signal_priority` is read by `SharedEntryPolicy.rank_key`, the gatekeeper's sort key (and htf_pivots' / trend_continuation's side pick): `(tier, primary + w x shared_context_score x unit, *metadata_fields, final_priority_score, activity, -rank)`.
  - `primary_field` defaults to `final_priority_score`, which already contains the shared terms.
  - `shared_score_weight` (w) defaults to 0.
  - `rank_unit_field` is the metadata field that converts one shared-score point into the primary's units (default 1). top_tier stamps `regime_rank_unit = 1 / (regime ceiling - regime floor)` next to its normalised regime score.
  - `metadata_fields` is the tail.
  - `tier` puts divergence-only entries behind every strategy signal. A `strategy_priority_score` primary drops the `final_priority_score` tiebreak.
  - Unknown keys, duplicate fields, a negative or boolean weight, or a unit field without a positive weight fail at load.
  - Shipped: top_tier / small_cap_squeeze `regime_score_normalized` with weight 1.0 and `regime_rank_unit`; the four peers `ltf_score` with weight 0.5 and their seven-field tail; microcap_pm_breakout and both 0DTE strategies `strategy_priority_score` with weight 0; everyone else the default.

### Skip tokens and divergence-only entries

While `use_divergence_entry_signal` is on, a symbol the strategy produced no signal for may be entered on a divergence, unless the strategy's recorded decision says it never evaluated a setup there. That judgement reads the reasons' tokens (the part before `(`) against `shared_entry.DIVERGENCE_INELIGIBLE_REASONS`, plus any `insufficient_*` token. Record eligibility skips with the standard tokens: `symbol_not_tradable`, `already_in_position`, `underlying_already_open`, `outside_entry_window`, `after_entry_cutoff`, `extended_hours_not_eligible`, `session_empty`, `last_close_invalid`, `missing_ltf_context`, `opening_range_incomplete`, `opening_range_values_nan`, `pm_reference_pmh_invalid`, `event_blackout`, `earnings_blackout`, `shorts_disabled`, `relative_strength_lagging_sector` and `relative_strength_leading_sector`. A new eligibility token must be added to that set, or divergence-only entries will open on symbols the strategy refused to look at.

### What a new strategy gets without calling anything

- Every `shared_entry` veto, both refinements, the FVG retest, every score term, the score floor and the divergence confirmation.
- Divergence-only entries, unless the manifest opts out.
- Every `shared_exit` family, the time stop, the tape confirmation and partial closes.
- The ORB / pullback graces when its style family is `orb` / `pullback`.
- The ranking (the default `final_priority_score`, or its manifest declaration).
- The shared metadata stamps and their audit logging.
- A scaffolded preset that starts from the config dataclass defaults of those sections.

To keep it that way, run `tests/test_shared_knob_contract.py`, and add the strategy's scenario to `_SCENARIOS` in `tests/test_knob_reach_matrix.py`. That matrix switches every veto, the score floor, every score term, both refinements and every exit family on and off for every registered strategy, and fails until the new one has a scenario.

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
13. Build every entry through `self.entry_policy` (`EntryProposal` -> `admit` -> `emit`) and put strategy-only exits in `strategy_exit_signal`
14. Set `strategy: <name>` in YAML to use it
15. Run `tests/test_shared_knob_contract.py`, add the strategy's scenario to `tests/test_knob_reach_matrix.py`, and smoke-test the plugin before production use

A scaffold generator is included now:

```bash
python scripts/scaffold_strategy_plugin.py my_new_strategy
```

That command creates a new plugin directory with `manifest.json`, `strategy.py`, `screener.py`, and `__init__.py`, and also writes a matching top-level runtime preset at `configs/config.<strategy>.yaml`. The generated `strategy.py` already proposes / admits / emits and passes the contract test. The preset is cloned from `configs/config.example.yaml`, except that its `shared_entry` and `shared_exit` sections, `risk.time_stop_minutes` and `support_resistance.entry_proximity_scoring_enabled` are the config dataclass defaults (2026-09-24). The example carries `peer_confirmed_htf_pivots` parity values there, which a new strategy should not inherit.

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

Avoid `from ..shared import *`. Import only the names your plugin uses. Entries go through the shared entry stage (see the contract above); the strategy never constructs a `Signal` itself.

Read the clock through its module: `from ... import sessions`, then `sessions.now_et()`. `_strategies/shared.py` does not re-export `now_et`, and a name bound with `from ... import now_et` would escape the tests' clock pin (`tests/support/clock.freeze_et`); `tests/test_module_layering.py` rejects it.


```python
from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    _safe_float,
    insufficient_bars_reason,
    pd,
)
from ..shared_entry import EntryProposal
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
            # Eligibility skips use the standard tokens, so a divergence-only
            # entry never opens a symbol this strategy did not evaluate.
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue

            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(
                    c.symbol,
                    "skipped",
                    [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)],
                )
                continue

            last = frame.iloc[-1]
            close = _safe_float(last.get("close"), 0.0)
            vwap = _safe_float(last.get("vwap"), close)
            day_strength = _safe_float(c.metadata.get("change_from_open"), 0.0)
            rvol = _safe_float(c.metadata.get("relative_volume_10d_calc"), 0.0)

            side = Side.SHORT if (allow_short and close < vwap and day_strength < 0) else Side.LONG
            # The setup's own blockers become the proposal's pending reasons,
            # so a refusal lists them together with every shared veto.
            reasons: list[str] = []
            if rvol < min_rvol:
                reasons.append("rvol_too_low")
            if side == Side.LONG and not (close > vwap and day_strength > 0):
                reasons.append("no_setup")

            proposal = EntryProposal(
                candidate=c,
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
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue

            # Everything after admission reads the ADMITTED (refined) levels.
            management = self._adaptive_management_components(
                side,
                close,
                admitted.stop,
                admitted.target,
                style="trend",
                runner_allowed=False,
                continuation_bias=float(admitted.fvg["fvg_continuation_bias"]),
            )
            signal = self.entry_policy.emit(
                admitted,
                reason="my_new_strategy",
                # The strategy's own priority, WITHOUT the shared terms: emit
                # adds shared_context_score to make final_priority_score.
                strategy_score=1.0 + float(c.activity_score) * 0.15,
                management=management,
                target=admitted.target,
                metadata={"rvol": rvol, "day_strength": day_strength},
            )
            self._record_entry_decision(c.symbol, "signal", [signal.reason])
            out.append(signal)

        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
```

`emit` stamps `entry_price` (which `risk.py::_signal_entry_price` reads for the same-level retry block and the fib override), `final_priority_score`, `entry_style_family` and the shared context fields, so the strategy's own metadata holds only its own keys. Exits need no code: the shared exit families run for every position, and a strategy-only exit goes in `strategy_exit_signal(...)`.

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

## Manifest window validation (2026-09-19)

`entry_windows` / `management_windows` / `screener_windows` are validated at
manifest LOAD time, not just for shape. Each `start` and `end` must parse
through `parse_hhmm`, and the rejection names the field, the index, which end
failed and the offending value:

```
manifest.json:entry_windows[0] start '9am' is not a valid HH:MM time: ...
```

Previously only the shape was checked — a list of two non-empty values — so
`"9am"`, or a bare integer (which `str()` turns into `"930"`), passed load and
`parse_hhmm` raised inside a trading cycle instead. Load-time validation exists
so a manifest typo fails before capital is at risk, which means the check
belongs at load.

**An overnight window is still valid.** `["22:00", "02:00"]` wraps past midnight
by design — see `Window.contains` in `models.py` — so `start > end` is a
legitimate configuration and is deliberately not rejected.

Two things remain accepted on purpose: a missing `schema_version` defaults to 1
for backward compatibility, and a non-string `strategy_class` is caught later
when the class is resolved rather than at manifest load.
