# Pairs Residual

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **relative-value / residual divergence strategy**. It does not mainly care whether a stock is simply up or down. It cares whether one symbol is becoming meaningfully stronger or weaker than its configured reference symbol, and whether that relative move has reached a tradable extreme.

### 1. The watchlist comes from explicit configured pairs

This strategy does not scan the general market the way the single-name stock strategies do. Instead, it starts from the configured `pairs` list.

For each pair, it pulls both:

- the traded symbol
- the reference symbol

Then it builds a “focus score” from the relative gap in session strength and the traded symbol’s relative volume.

### 2. It only looks at the traded leg, but always in relation to the reference leg

The screener keeps a candidate only when:

- the traded symbol has enough relative volume
- the traded symbol has enough session strength in absolute terms
- the pair actually exists in the live screener result

The side bias comes from either:

- the configured `side_preference`, or
- the sign of the relative strength gap if both sides are allowed

### 3. The signal is based on a relative-performance z-score

Once the bars are loaded, the strategy aligns the two close series and calculates:

- percentage returns for both legs
- a rolling cumulative relative-return series
- a z-score of the latest relative deviation versus the recent sample

That z-score is the core trade trigger.

- high positive z-score -> the traded symbol is relatively strong -> long candidate
- high negative z-score -> the traded symbol is relatively weak -> short candidate if shorts are enabled

The strategy also blocks entries if the z-score is **too** extreme, so it is not trying to chase an already blown-out pair.

### 4. Directional structure still matters

Even though the entry is based on pair divergence, the actual traded symbol still has to pass the normal context filters. The side the z-score picks becomes one proposal to the shared entry stage (`entry_policy.admit`, `_strategies/shared_entry.py`; style and family `pairs`), built on the traded leg alone: every context, veto, refinement and score term reads the leg's 1-minute frame and the leg's symbol, never the reference's. The setup's own blocker (z-score too extended) and the exhaustion checks are its pending reasons, and the refusal records them together with every veto that fired. With neither side ready (z-score below the threshold, side or shorts not allowed) the skip is recorded without a proposal.

- the `shared_entry` vetoes the preset switches on: the shipped `config.pairs_residual.yaml` runs the structure filter (`use_structure_filter`) only; S/R, broken-level, chart, dual-divergence and candle vetoes are off
- exhaustion checks
- support/resistance refinement (`use_sr_stop_target_refinement`)
- technical-level refinement (`use_technical_stop_target_refinement`)
- the shared score terms (FVG, technical, S/R proximity, HTF divergence)

So this is not a pure statistical-arbitrage bot. It is closer to **relative-strength timing with single-name execution discipline**.

The manifest turns the shared divergence-only entries off (`capabilities.shared_entry.divergence_entry: false`): a pair trade needs its z-score, so `use_divergence_entry_signal` never opens a leg on a divergence alone.

### 5. Stop, target, and management

The initial stop/target start from the shared stock-risk defaults for the traded symbol (`risk.default_stop_pct` / `default_target_pct`). The shared stage then refines them through the S/R and technical layers, and the management, runner and signal read the refined levels. The signal carries the reference as `reference_symbol` and `pair_id` = `<leg>:<reference>`.

Signal strength (`final_priority_score`, the rank) is:

- the absolute z-score × 100
- the screener focus score × 0.25
- structure bonuses (the leg's structure bias, a recent break of structure)
- plus the shared context score (`shared_context_score`: FVG and entry-context terms), stamped separately since 2026-09-24; the total is unchanged

Runner behavior is only enabled when the z-score is still tradable rather than already too stretched, and when the continuation/FVG context supports it.

### 6. What a good setup looks like

A strong pairs-residual setup usually means:

- the traded symbol is materially outperforming or underperforming its reference
- the divergence is real enough to matter, but not so extreme that it already looks exhausted
- the traded chart’s local structure agrees with the trade direction
- the normal entry context is not fighting the trade

In plain English:

**“This strategy asks whether one symbol is becoming unusually strong or weak relative to its partner, then only trades that divergence when the traded leg’s own chart still supports the move.”**

## Shipped reference

Purpose: trade one side of a configured pair when the primary symbol diverges enough from its reference symbol.

Default windows:

The shipped top-level preset also includes two editable example entries in the top-level `pairs:` block so the strategy can start producing candidates immediately. Replace those examples with your own intended pairs.


- `entry_windows`: `[['10:10', '14:10']]`
- `management_windows`: `[['09:55', '15:20']]`
- `screener_windows`: `[['09:55', '14:10']]`

Strategy-specific knobs:

- `zscore_entry`: minimum residual z-score required to trigger.
- `max_zscore_entry`: do not chase if the residual is already too stretched.
- `lookback_bars`: rolling history window used in the residual/z-score calculation.
- `min_rvol`: minimum relative volume for the traded symbol.
- `min_day_strength`: minimum whole-percent session-strength threshold for the primary symbol.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- stock FVG confluence
- adaptive stock trade management

Current code defaults:

| Option                            | Current code default            |
|-----------------------------------|---------------------------------|
| `zscore_entry`                    | `1.25`                          |
| `max_zscore_entry`                | `2.1`                           |
| `lookback_bars`                   | `90`                            |
| `min_rvol`                        | `1.8`                           |
| `min_day_strength`                | `3.0`                           |
| `entry_exhaustion_filter_enabled` | `true`                          |
| `max_entry_vwap_extension_atr`    | `0.95`                          |
| `max_entry_ema9_extension_atr`    | `0.82`                          |
| `max_entry_bar_range_atr`         | `1.65`                          |
| `max_entry_upper_wick_frac`       | `0.3`                           |
| `max_entry_lower_wick_frac`       | `0.3`                           |
| `entry_wick_close_position_guard` | `0.62`                          |
| `htf_fvg_entry_weight`            | `0.28`                          |
| `ltf_fvg_entry_weight`     | `0.16`                          |
| `opposing_fvg_entry_penalty_mult` | `0.95`                          |
| `fvg_runner_rr_bonus`             | `0.1`                           |
| `adaptive_breakeven_rr`           | `0.86`                          |
| `adaptive_profit_lock_rr`         | `1.08`                          |
| `adaptive_profit_lock_stop_rr`    | `0.22`                          |
| `adaptive_runner_trigger_rr`      | `1.1`                           |
| `force_flatten`                   | `{'long': True, 'short': True}` |
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.pairs_residual.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
