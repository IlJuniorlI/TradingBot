# 0DTE ETF Long Options

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **0DTE long-premium ETF strategy**. It trades a single long call or long put only. It does **not** sell naked premium and it does **not** open spreads. That makes it the simpler and more directional of the two 0DTE ETF strategies in the package.

### 1. It begins by classifying the underlying regime

The strategy does not start with the option chain. It starts with the underlying ETF and asks what kind of day is in progress. The regime logic looks at things like:

- opening-range behavior
- VWAP and EMA relationships
- short-horizon returns
- confirmation bars
- chop / range behavior
- optional higher-timeframe context

Only after the underlying regime passes does the strategy attempt to build an option trade.

### 2. It supports two directional long-premium styles

Depending on the regime and time of day, the strategy can attempt:

- **ORB long option** logic for early directional expansion
- **trend long option** logic for later continuation

For bullish conditions it looks for a long call. For bearish conditions it looks for a long put. It does not mix both sides at once.

### 3. It blocks a lot of bad environments before touching the chain

Before building the option signal, the strategy can skip the trade because of:

- event blackout windows
- entry cutoff time
- an existing open option tied to the same underlying
- insufficient underlying bars
- regime classification failure
- the shared entry vetoes, on both styles (see "Shared entry stage" below): the LTF market structure against the trade's direction and a crowded S/R level (both shipped on), plus the chart / candle / divergence / broken-level vetoes when their `shared_entry` knobs are switched on

That is by design. It wants the underlying day type to be right first, then the option implementation second.

### 4. Then it has to find a usable contract

Once the underlying setup is valid, the strategy filters for a same-day contract that meets the configured quality rules. If it cannot find a clean enough contract or quote, the trade is skipped even if the underlying chart looked good.

### 5. How the option trade is framed

Because this is long premium, the risk model is simpler:

- the debit paid is the maximum contract loss
- the stop is based on a fraction of the premium
- the target is based on a multiple of the premium
- the levels are clamped so the contract-level stop/target remain sensible

The resulting signal still carries metadata about the underlying regime and the selected option so the engine can manage it coherently.

### 6. What a strong setup looks like

A strong long-premium setup usually means:

- the underlying ETF has a clean directional regime
- the time-of-day style matches that regime
- the option chain offers a clean enough contract to express it
- the trade is not blocked by blackout windows or stale quote conditions

In plain English:

**“This strategy first asks whether the ETF itself is having the right kind of day, then buys a same-day call or put only if the option contract is liquid enough to express that directional view cleanly.”**

## Shipped reference

Purpose: 0DTE ETF strategy that buys a single long call or long put and never sells naked premium or opens a spread.

Default windows:

- `entry_windows`: `[['09:40', '14:10']]`
- `management_windows`: `[['09:30', '15:20']]`
- `screener_windows`: `[['09:35', '14:10']]`

Long-option-only parameters:

- `options.styles` can selectively enable `orb_long_option` and `trend_long_option`.
- `long_option_min_trend_score`: stricter trend-score threshold before buying the option.
- `long_option_min_score_gap`: stricter score-gap requirement before buying the option.
- `long_option_max_vwap_extension_pct`: max extension from VWAP before the long option is considered too late.
- `long_option_max_ema_gap_pct`: max EMA gap before the long option is considered too extended.
- `long_option_max_ret5` / `long_option_max_ret15`: short-horizon spike filters that reduce chase entries.

Current code defaults:

| Option                               | Current code default |
|--------------------------------------|----------------------|
| `orb_start_time`                     | `09:35`              |
| `orb_end_time`                       | `10:05`              |
| `orb_opening_window_start`           | `09:30`              |
| `orb_opening_window_end`             | `09:34`              |
| `orb_opening_min_bars`               | `3`                  |
| `trend_start_time`                   | `10:05`              |
| `trend_end_time`                     | `13:30`              |
| `no_new_entries_after`               | `13:45`              |
| `min_bars`                           | `90`                 |
| `min_confirm_bars`                   | `30`                 |
| `trend_vwap_lookback`                | `10`                 |
| `flip_lookback`                      | `14`                 |
| `range_lookback`                     | `25`                 |
| `trend_vwap_distance_pct`            | `0.0014`             |
| `trend_ema_gap_pct`                  | `0.0007`             |
| `trend_above_vwap_frac`              | `0.74`               |
| `trend_min_ret5`                     | `0.0006`             |
| `trend_min_ret15`                    | `0.0013`             |
| `range_vwap_distance_pct`            | `0.0018`             |
| `range_ema_gap_pct`                  | `0.0007`             |
| `range_max_intraday_move_pct`        | `0.009`              |
| `credit_max_day_move_pct`            | `0.008`              |
| `credit_max_vix_change_pct`          | `0.01`               |
| `chop_flip_min`                      | `4`                  |
| `chop_flip_max_for_trend`            | `3`                  |
| `chaos_intraday_range_pct`           | `0.016`              |
| `min_trend_score`                    | `5.1`                |
| `min_range_score`                    | `4.6`                |
| `min_score_gap`                      | `1.6`                |
| `long_option_min_trend_score`        | `5.0`                |
| `long_option_min_score_gap`          | `1.5`                |
| `long_option_max_vwap_extension_pct` | `0.0026`             |
| `long_option_max_ema_gap_pct`        | `0.0015`             |
| `long_option_max_ret5`               | `0.002`              |
| `long_option_max_ret15`              | `0.0044`             |
| `orb_breakout_buffer_pct`            | `0.0008`             |
| `require_index_confirmation`         | `true`               |
| `candle_weight`                      | `0.5`                |
| `candle_sr_weight`                   | `0.35`               |
| `candle_trend_follow_weight`         | `0.25`               |
| `candle_range_penalty`               | `0.3`                |
| `candle_mixed_penalty`               | `0.18`               |
| `use_htf_trend_confirmation`         | `true`               |
| `require_htf_alignment`              | `true`               |
| `htf_minutes`              | `15`                 |
| `htf_lookback_days`                  | `60`                 |
| `htf_min_bars`                       | `20`                 |
| `htf_vwap_distance_pct`              | `0.0009`             |
| `htf_ema_gap_pct`                    | `0.0007`             |
| `htf_min_ret3`                       | `0.0009`             |
| `htf_range_vwap_distance_pct`        | `0.002`              |
| `htf_range_ema_gap_pct`              | `0.001`              |
| `htf_score_bonus`                    | `0.65`               |
| `htf_score_penalty`                  | `0.65`               |
| `fvg_context_weight_scale`           | `0.9`                |

## Shared 0DTE regime engine

Both option strategies use a regime engine that mixes ORB timing, trend scoring, range/chop scoring, optional index confirmation, HTF confirmation, weighted 1/2/3-bar candle context, FVG context, and option-chain quality filters.

Common parameter families:

- Session clock:
  - `orb_start_time`, `orb_end_time`, `orb_opening_window_start`, `orb_opening_window_end`, `orb_opening_min_bars`, `trend_start_time`, `trend_end_time`, `no_new_entries_after`
  - The **trading window** (`orb_start_time` → `orb_end_time`) determines when ORB entries are eligible to fire. The **opening window** (`orb_opening_window_start` → `orb_opening_window_end`) determines the bars used to derive `or_high` / `or_low` — the levels the breakout is measured against. Default keeps the legacy behaviour (09:30-09:34 opening, 09:35-10:05 trading) but the two are decoupled — extending the trading window without extending the opening window means later breakouts are measured against an unchanging early-session reference.
  - `orb_opening_min_bars` (default `3`) requires at least N bars in the opening window before deriving or_high/or_low. Guards against the degenerate case where a single 09:34 bar is treated as the "opening range."
- Structure / S/R vetoes (both styles): `shared_entry.use_structure_filter` and `shared_entry.use_sr_filter` (both `true` in the preset). They replaced the ORB path's `orb_apply_structure_veto` / `orb_apply_sr_veto` params on 2026-09-24; a preset that still sets either fails at load, naming its replacement. Set both knobs `false` for the pre-2026-05-14 "fire on any breakout that clears or_high/or_low" behaviour (that also drops them from the trend style, which always ran them).
- Minimum data:
  - `min_bars`, `min_confirm_bars`, `trend_vwap_lookback`, `flip_lookback`, `range_lookback`
- Live tape filters (replace legacy TV cumulative RVOL — 2026-05-14):
  - `min_activity_for_entry`, `trend_activity_threshold`, `credit_activity_min`, `credit_activity_max`. See parent `zero_dte_etf_options/README.md` for the full description.
- Trend scoring:
  - `trend_vwap_distance_pct`, `trend_ema_gap_pct`, `trend_above_vwap_frac`, `trend_min_ret5`, `trend_min_ret15`
- Range / chop scoring:
  - `range_vwap_distance_pct`, `range_ema_gap_pct`, `range_max_intraday_move_pct`, `credit_max_day_move_pct`, `credit_max_vix_change_pct`, `chop_flip_min`, `chop_flip_max_for_trend`, `chaos_intraday_range_pct`, `min_range_score`
- Regime separation:
  - `min_trend_score`, `min_score_gap`
- ORB confirmation:
  - `orb_breakout_buffer_pct`
- Index confirmation:
  - `require_index_confirmation`
- Candle / SR / trend-follow weights:
  - `candle_weight`, `candle_sr_weight`, `candle_trend_follow_weight`, `candle_range_penalty`, `candle_mixed_penalty`
- HTF confirmation:
  - `use_htf_trend_confirmation`, `require_htf_alignment`, `htf_minutes`, `htf_lookback_days`, `htf_min_bars`, `htf_vwap_distance_pct`, `htf_ema_gap_pct`, `htf_min_ret3`, `htf_range_vwap_distance_pct`, `htf_range_ema_gap_pct`, `htf_score_bonus`, `htf_score_penalty`
- FVG contribution:
  - `fvg_context_weight_scale`

**Knobs that DON'T apply to the long-options strategy** (inherited
from parent but only meaningful for credit-spread builders):
`credit_distance_gate_enabled`, `min_credit_distance_atr`,
`credit_pivot_buffer_gate_enabled`,
`min_short_strike_pivot_buffer_atr`, `adaptive_width_enabled`,
`adaptive_width_max_scale`. The long-options yaml leaves all of
these at their defaults. They're listed in `OptionsConfig` so the
shared dataclass works for both strategies, but the long-only
entry path never reads them — there's no short leg to gate.

General behavior:

- Raising score thresholds makes the option engine more selective.
- Raising the HTF bonus/penalty makes HTF alignment matter more.
- Raising `fvg_context_weight_scale` makes one-minute and HTF FVG context matter more to the regime score.
- Tightening the trend-extension caps reduces late chase entries.

## Shared entry stage (2026-09-24)

Every `shared_entry` knob is applied by the shared entry stage
(`_strategies/shared_entry.py`, `SharedEntryPolicy`); the strategy no longer
reads any of them itself.

- Both styles hand a *premium* proposal (family `option_long`, the underlying's
  frame, no price stop / target) to `entry_policy.admit` in
  `_build_single_option_signal`, before the chain is read. The proposal's
  direction is the underlying's: a long call is LONG, a long put SHORT, while
  both orders are buys (`emit(order_side=LONG)`), so the vetoes read the side
  the trade needs the underlying to go.
- The ORB path's own structure / S/R vetoes are gone. They were an `elif`, with
  an `orb_long_option_` prefix on the reason, so a structure veto hid an S/R one.
  The shared vetoes are recorded under their shared tokens, every blocker
  listed (`market_structure_bearish(...)`, `too_close_to_htf_resistance(...)`).
- The trend style's own blockers (`_long_option_style_gate`: conviction, score
  gap, VWAP / EMA extension, 5 / 15-bar spike) are the proposal's pending
  reasons. A refusal lists them first, then the shared vetoes. The gate itself
  no longer runs the structure / S/R checks.
- The regime engine is the parent's: its LTF structure veto moved into the
  stage, its HTF structure-bias veto stays (see `zero_dte_etf_options/README.md`).
- Ranking is on `strategy_priority_score` (activity + the traded regime's score
  and margin), with shared weight 0. `final_priority_score` adds the shared
  context score (the FVG entry term with the shipped knobs), for the logs only.
  No divergence-only entries (`capabilities.shared_entry.divergence_entry: false`).

## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.zero_dte_etf_long_options.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.

## Same-level retry block on an option (2026-09-25)

`risk.same_level_block_minutes` keys an option signal on the **underlying**: its
market direction (`metadata['direction']`) and its price at entry
(`underlying_entry`), against `same_level_block_atr_mult` x the underlying's ATR
at exit. Until then it compared premiums with that ATR, so it could only fire on
an exact premium match. Every long call and long put is BOUGHT, so the old
order-side match could never tell a call from a put; the direction now does
(a long call is bullish, a long put bearish). The preset ships
`same_level_block_minutes: 0` (parity: the old block never fired on an option in
the archive); turning it on is a go-live decision for a beta dry-run. See
`zero_dte_etf_options/README.md` for the detail.
