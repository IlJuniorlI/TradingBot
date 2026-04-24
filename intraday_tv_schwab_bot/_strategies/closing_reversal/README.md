# Closing Reversal

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **late-day, long-only rebound strategy**. It is not trying to buy random weakness. It is specifically looking for names that were already strong during the day, pulled back from the highs in a controlled way, and are now showing evidence that buyers are stepping back in before the close.

### 1. It starts with a strong-mover watchlist

The screener only looks for names that already have:

- the small-cap / liquid-equity filters from the shared screener base
- enough relative volume
- positive session-aware move from the active session open inside the configured strength band

Every candidate is ranked by session strength, and the side bias is always **long**. So this strategy assumes the stock has already proven itself during the active session before the entry logic even begins.

### 2. It checks that the pullback is controlled, not broken

Once a symbol is on the list, the strategy checks whether the recent pullback still looks healthy:

- the symbol must still have strong active-session strength
- the pullback from the recent session high cannot be too deep
- the last three bars should show a short-term bounce in price

That means it is trying to buy a **dip in strength**, not a collapsing late-day fade.

### 3. It wants actual reversal evidence

The entry does not trigger just because price is down from the highs. The strategy also wants reversal quality from the last few bars and the chart context. Candles now use the shared weighted 1/2/3-bar model, so a stronger 3-bar reversal outranks a weaker 1-bar hint:

- bullish candlestick patterns in the recent bars
- bullish reversal or continuation context from the chart-pattern layer
- price back above EMA9
- a strong close location on the trigger bar
- optional positive short-horizon momentum confirmation (`ret5`)

So the setup is really: **strong day -> orderly pullback -> reversal evidence -> reclaim of short-term control**.

### 4. It still passes through shared safety filters

Even when the local reversal logic looks good, the trade can still be blocked by the shared context layers:

- opposing chart-pattern filter
- 1-minute market-structure veto
- support/resistance veto
- technical-level refinements
- FVG-based context adjustments

That is why a name can look visually interesting but still be skipped: the strategy is trying to avoid buying late-day bounces directly into poor structure or nearby resistance.

### 5. Stop, target, and management

The initial stop starts under the recent three-bar low. The first target points back toward the session-high area, capped by the default target logic if needed. After that, the signal is refined by:

- support/resistance
- technical levels
- FVG context
- adaptive management metadata

Because this is a **reversal-style** setup, the strategy does not automatically treat it like a full continuation runner. It is more conservative than a trend-following breakout system.

### 6. What a good setup looks like

The best closing-reversal setup usually looks like this:

- a stock was strong for most of the day
- it pulled back, but did not fully lose the trend
- buyers start showing up again in the final stretch
- the trigger bar reclaims short-term momentum and closes well
- the bounce is not running directly into obvious resistance

In plain English, this strategy is saying:

**“I want a strong stock that dipped late, held together, and is now proving that the late-day rebound is real enough to trade into the close.”**

## Shipped reference

Purpose: late-day rebound in strong names that have pulled back but are showing reversal quality.

Default windows:

- `entry_windows`: `[['15:33', '15:54']]`
- `management_windows`: `[['15:10', '15:57']]`
- `screener_windows`: `[['15:10', '15:52']]`

Strategy-specific knobs:

- `min_day_strength` / `max_day_strength`: whole-percent session-strength bounds using the canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `max_pullback_from_high`: max allowed pullback from the recent high.
- `min_reversal_close_position`: reversal-candle close quality requirement.
- `require_positive_reversal_ret5`: require positive short-horizon confirmation when enabled.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current code defaults:

| Option                            | Current code default            |
|-----------------------------------|---------------------------------|
| `min_day_strength`                | `6.0`                           |
| `max_day_strength`                | `16.5`                          |
| `min_rvol`                        | `2.5`                           |
| `max_pullback_from_high`          | `0.05`                          |
| `min_reversal_close_position`     | `0.6`                           |
| `require_positive_reversal_ret5`  | `true`                          |
| `htf_fvg_entry_weight`            | `0.38`                          |
| `one_minute_fvg_entry_weight`     | `0.24`                          |
| `opposing_fvg_entry_penalty_mult` | `0.88`                          |
| `fvg_runner_rr_bonus`             | `0.12`                          |
| `adaptive_breakeven_rr`           | `0.72`                          |
| `adaptive_profit_lock_rr`         | `0.98`                          |
| `adaptive_profit_lock_stop_rr`    | `0.15`                          |
| `force_flatten`                   | `{'long': True, 'short': True}` |
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.closing_reversal.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
