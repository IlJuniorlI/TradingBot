# Mean Reversion

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **long-only pullback-and-reversal strategy for already strong names**. It is very similar in spirit to closing_reversal, but it is not tied to the final minutes of the session. It is meant to buy a controlled intraday dip after a strong name starts showing evidence of turning back up.

### 1. The screener starts with strong names, not weak names

The screener looks for symbols that already have:

- enough liquidity from the shared screener rules
- enough relative volume
- positive session-aware move from the active session open inside the configured range

So despite the name “mean reversion,” this is **not** a catch-a-falling-knife strategy. It only looks for pullbacks inside names that were already acting strong on the day.

### 2. It checks the pullback from the recent high

The core structural test is whether the symbol has pulled back from the recent 20-bar high, but not too much:

- it calculates the recent high
- measures the pullback percentage from that high
- blocks the trade if the pullback is too deep

That makes the setup more like **trend-pullback re-entry** than true statistical mean reversion.

### 3. It wants the pullback to start reversing now

After the pullback check, the strategy asks whether the stock is actually bouncing. Candle confirmation now comes from the shared weighted 1/2/3-bar model, so stronger multi-bar reversals carry more weight than single-bar hints:

- bullish candle patterns must be present in the recent bars
- chart-pattern context must be supportive
- price needs to be back above EMA9
- the trigger bar must close well within its range
- optional short-horizon return confirmation (`ret5`) must be positive

So the entry is only valid once the pullback starts to **resolve upward**, not while the dip is still unfolding.

### 4. Shared context can still veto the setup

If the local reversal conditions pass, the trade still goes through the shared context stack:

- opposing chart-pattern filter
- 1-minute structure filter
- support/resistance filter
- technical-level refinement
- FVG-based entry adjustments

That helps keep the strategy from buying strong names that are technically rebounding but still pressing into bad context.

### 5. How the trade is framed

The initial stop starts under the recent short-term swing low. The first target points back toward the recent high area. After that, the strategy refines both sides of the trade using:

- support/resistance
- technical levels
- FVG context
- adaptive management metadata

Its signal strength is built from the screener strength, the shallowness of the pullback, pattern quality, and shared context adjustments.

### 6. What a good setup looks like

A strong mean-reversion setup here usually means:

- the stock was strong first
- the pullback stayed controlled
- the recent candles show reversal quality
- price is reclaiming EMA9 instead of staying below it
- structure and context are no longer fighting the long

In plain English:

**“This strategy wants a strong stock that pulled back just enough to reset, then started turning back up before the original trend was truly damaged.”**

## Shipped reference

Purpose: buy pullbacks in strong names after bullish reversal evidence appears.

Default windows:

- `entry_windows`: `[['09:39', '10:55'], ['13:07', '14:45']]`
- `management_windows`: `[['09:34', '15:10']]`
- `screener_windows`: `[['09:39', '10:55'], ['13:07', '14:45']]`

Strategy-specific knobs:

- `min_day_strength` / `max_day_strength`: whole-percent session-strength bounds using the canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `max_pullback_from_high`: max allowed pullback from the recent high.
- `min_reversal_close_position`: minimum candle close-position quality required for the reversal candle.
- `require_positive_reversal_ret5`: when `true`, require short-horizon return confirmation for the reversal.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current code defaults:

| Option                            | Current code default            |
|-----------------------------------|---------------------------------|
| `min_day_strength`                | `5.2`                           |
| `max_day_strength`                | `15.5`                          |
| `min_rvol`                        | `2.4`                           |
| `max_pullback_from_high`          | `0.027`                         |
| `min_reversal_close_position`     | `0.58`                          |
| `require_positive_reversal_ret5`  | `true`                          |
| `htf_fvg_entry_weight`            | `0.34`                          |
| `one_minute_fvg_entry_weight`     | `0.2`                           |
| `opposing_fvg_entry_penalty_mult` | `0.88`                          |
| `fvg_runner_rr_bonus`             | `0.12`                          |
| `adaptive_breakeven_rr`           | `0.72`                          |
| `adaptive_profit_lock_rr`         | `0.96`                          |
| `adaptive_profit_lock_stop_rr`    | `0.15`                          |
| `force_flatten`                   | `{'long': True, 'short': True}` |
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.mean_reversion.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
