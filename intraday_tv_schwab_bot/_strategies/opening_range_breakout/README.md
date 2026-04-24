# Opening Range Breakout

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **long-only opening-range breakout strategy**. It is designed to find names that are already active before or just after the bell, define the opening range, and then enter only if price breaks out of that range with supportive trend context.

### 1. It can build the watchlist before the open or during the open

The screener has a dedicated watchlist mode:

- `premarket` freezes the watchlist from premarket strength
- `early_session` lets screening continue into the open
- `none` disables the special watchlist behavior

The point is to start with names that are already active instead of scanning the whole universe after the open.

### 2. It waits for the opening range to be fully formed

The strategy will not evaluate a symbol until it has enough same-day bars to define the opening range. It then computes:

- opening-range high
- opening-range low
- the “after opening range” bars where breakouts are allowed

That prevents premature entries before the ORB box is actually complete.

### 3. The entry is a true breakout, not just a touch

For a long, price must break above the opening-range high by a configured buffer. On top of that, the strategy also wants:

- price above VWAP
- EMA9 above EMA20
- supportive chart-pattern context
- a decent trigger-bar close

So the entry is not just “price touched OR high.” It is **breakout plus directional confirmation**.

### 4. It uses anti-chase logic heavily

Because ORB setups can get crowded fast, the strategy runs them through several quality filters:

- opposing chart-pattern filter
- FVG retest deferment logic
- exhaustion / extension checks
- divergence checks
- 1-minute structure filter
- support/resistance veto

This is one of the main reasons an apparent ORB can still be skipped: the code is trying to separate clean opening expansion from low-quality chasing.

### 5. How the trade is framed

The opening-range low is the natural initial stop anchor. The first target starts from a reward-to-risk projection off the breakout. Then the strategy refines stop and target using:

- support/resistance
- technical levels
- FVG context
- adaptive management metadata

If the FVG continuation context is strong enough, the strategy can also mark the trade as a better candidate for runner-style management.

### 6. What a good setup looks like

The best ORB setup usually looks like this:

- the name was active before the bell
- the first few minutes create a tight but meaningful opening box
- price breaks above the box instead of failing inside it
- VWAP and EMA alignment support the breakout
- the move is not already too extended when the trigger appears

In plain English:

**“This strategy wants a stock that comes into the open already in play, defines a clean opening battleground, and then expands through that battleground with real follow-through.”**

## Shipped reference

Purpose: opening-range breakout in active small-cap names.

Default windows:

- `entry_windows`: `[['09:37', '10:05']]`
- `management_windows`: `[['09:30', '10:50']]`
- `screener_windows`: `[['08:10', '09:29']]`

Strategy-specific knobs:

- `orb_watchlist_mode`: valid values are `premarket`, `early_session`, or `none`.
  - `premarket`: build the watchlist before 09:30 ET and keep it frozen during the ORB window.
  - `early_session`: let watchlist logic continue into the open, if the screener windows also extend into RTH.
  - `none`: skip the watchlist-strength filter and just use the ORB entry logic.
- `watchlist_min_change`: watchlist strength threshold in whole percent units.
- `watchlist_min_volume`: watchlist volume threshold.
- `opening_range_minutes`: size of the opening range in minutes.
- `min_breakout_buffer_pct`: extra percentage buffer above/below the opening range before entry.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- anti-chase FVG retest defer logic
- stock FVG confluence
- adaptive stock trade management

Current code defaults:

| Option                                            | Current code default            |
|---------------------------------------------------|---------------------------------|
| `orb_watchlist_mode`                              | `premarket`                     |
| `watchlist_min_change`                            | `5.5`                           |
| `watchlist_min_volume`                            | `800000`                        |
| `opening_range_minutes`                           | `5`                             |
| `min_breakout_buffer_pct`                         | `0.0011`                        |
| `entry_exhaustion_filter_enabled`                 | `true`                          |
| `max_entry_vwap_extension_atr`                    | `0.85`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.45`                          |
| `max_entry_upper_wick_frac`                       | `0.25`                          |
| `max_entry_lower_wick_frac`                       | `0.25`                          |
| `entry_wick_close_position_guard`                 | `0.68`                          |
| `anti_chase_fvg_retest_enabled`                   | `true`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `4`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.0028`                        |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.0019`                        |
| `anti_chase_fvg_retest_min_close_position`        | `0.66`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.15`                          |
| `htf_fvg_entry_weight`                            | `0.46`                          |
| `one_minute_fvg_entry_weight`                     | `0.28`                          |
| `opposing_fvg_entry_penalty_mult`                 | `1.0`                           |
| `fvg_runner_rr_bonus`                             | `0.2`                           |
| `adaptive_breakeven_rr`                           | `0.86`                          |
| `adaptive_profit_lock_rr`                         | `1.18`                          |
| `adaptive_profit_lock_stop_rr`                    | `0.26`                          |
| `adaptive_runner_trigger_rr`                      | `1.1`                           |
| `force_flatten`                                   | `{'long': True, 'short': True}` |
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.opening_range_breakout.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
