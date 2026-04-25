# Peer Confirmed Key Levels 1M

This strategy is the 1-minute trigger variant of `peer_confirmed_key_levels`.

It keeps the same hourly key-level / zone selection, peer confirmation, optional
macro confirmation, and ladder-aware trade management, but it evaluates the
actual trigger on the **1-minute timeframe** instead of the 5-minute trigger
frame used by the base strategy. It also inherits the base strategy's capped
trigger-quality bonus layer, so clean 1-minute reclaims / rejects, strong trigger
candles, expanding volume, and expanding range can outrank weaker triggers
without raising the minimum trigger gate.

## Intent

This variant is tuned for **faster entries with a compromise between aggressive
and balanced confirmation** around the same higher-timeframe battlegrounds. The
trigger reacts faster to reclaim / reject behavior, sweep activity, and
micro-structure confirmation, while the higher-timeframe level map remains
unchanged.

## What changes vs the base strategy

- `trigger_timeframe_minutes: 1`
- faster runtime / screener refresh defaults
- slightly tighter level zones and stop buffers
- lower minimum R:R and softer peer gate for earlier participation
- heavier weight on 1-minute FVG continuation context

## Core defaults

- Entry windows: `[["07:05", "15:40"]]`
- Management windows: `[["07:00", "15:58"]]`
- Screener windows: `[["07:00", "15:40"]]`
- HTF timeframe: `60m`
- Trigger timeframe: `1m`
- Minimum level score: `2.5`
- Minimum trigger score: `2.4`
- Trigger quality bonus: enabled, capped at `+2.0` total
- Minimum R:R: `1.6`
- Minimum peer agreement: `2`
- `level_score_raw_htf_weight: 0.65` (clamped to a 0.60 floor — same as base)

For the full parameter table — adaptive trade management knobs, FVG weights, force-flatten defaults, etc. — see [`../peer_confirmed_key_levels/README.md`](../peer_confirmed_key_levels/README.md). Anything not listed under "What changes vs the base strategy" inherits the base default.

Use `configs/config.peer_confirmed_key_levels_1m.yaml` for the shipped full runtime preset.
