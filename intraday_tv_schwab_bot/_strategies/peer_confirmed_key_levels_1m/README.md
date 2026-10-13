# Peer Confirmed Key Levels 1M

This strategy is the 1-minute trigger variant of `peer_confirmed_key_levels`.

It is the base strategy's class under its own name: the same hourly key-level /
zone selection, peer and macro confirmation (with their vote age limits: a
stale or missing vote counts against the side), the same 4-part base trigger gated by `min_ltf_score` (a CHoCH, a volume spike, the
candle model and a real sweep of the level) with the capped trigger-quality bonus
ranking on top, the entry spread guard, the protective exits on the bid / ask and
the force-flatten cutoff with its entry cutoff and its carried-position
flatten, and the same knob checks at load (base README, sections 4-7). It evaluates the trigger on the **1-minute timeframe** instead of the
5-minute one: the step frame's bars are complete, so each closed 1m bar is judged
once, on the first pass that sees it; that bar is the step frame's newest, so it
is never too late to judge (base README, section 4). Its preset runs `trade_management_mode:
adaptive`, so the ladder defence (`_ladder_exit_signal`) does not run there; the
trailing stop and the runner extension do.

## Intent

This variant is tuned for **faster entries with a compromise between aggressive
and balanced confirmation** around the same higher-timeframe battlegrounds. The
trigger reacts faster to reclaim / reject behavior, sweep activity, and
micro-structure confirmation, while the higher-timeframe level map remains
unchanged.

## What changes vs the base strategy

- `ltf_minutes: 1`
- faster runtime / screener refresh defaults
- slightly tighter level zones and stop buffers
- lower minimum R:R and level score for earlier participation (the peer gate is
  the base preset's: `min_peer_agreement: 3` in both shipped presets)
- heavier weight on 1-minute FVG continuation context
- the shipped preset runs the regular session only (entries 09:35-15:20,
  management 09:01-15:55), so its force-flatten cutoff is 15:50 (12:55 on an
  early-close day), both sides since 2026-10-13, and its entry cutoff 15:40
  (`entry_cutoff_lead_minutes`, 10; 12:45), after its entry window ends

It meets the shared entry stage exactly as the base does (2026-09-24): with a
1-minute trigger frame, the vetoes, the technical / chart / candle contexts and
the FVG term all read the 1-minute frame. `require_peer_target_clearance: true`
and `shared_score_weight: 0.5` as in the base; the preset keeps every shared
veto off (parity).

## Core defaults

- Entry windows: `[["07:05", "15:20"]]` (the preset: `09:35-15:20`)
- Management windows: `[["07:00", "15:58"]]` (the preset: `09:01-15:55`)
- Screener windows: `[["07:00", "15:40"]]` (the preset: `09:01-15:55`)
- HTF timeframe: `60m`
- Trigger timeframe: `1m`
- Minimum level score: `2.5`
- Minimum trigger score: `2.4`
- Trigger quality bonus: enabled, capped at `+2.0` total
- Minimum R:R: `1.6`
- Minimum peer agreement: `2` (the preset: `3`)
- `level_score_raw_htf_weight: 0.65` (a number in [0, 1], checked at load — same as base)
- Vote age limits: `peer_max_age_minutes: 3`, `macro_max_age_minutes: 20`
- Entry spread guard: `max_entry_spread_r` / `max_entry_spread_target_frac` `0` (off; the preset `0.33` / `0.5`)
- `protective_exits_on_executable_side: false` (the preset `true`)
- The base strategy's management and ladder reads (`force_flatten_buffer_minutes` 5, `adaptive_breakeven_offset_r` 0.05, the partial breakeven off, the runner's target and trail derived, the divergence ladder's 1.2 / 0.5 / 4): declared in the manifest and checked at load, as the base's
- `entry_cutoff_lead_minutes: 10`: no side opens within 10 minutes of its force-flatten cutoff, as the base's

For the full parameter table — adaptive trade management knobs, FVG weights, force-flatten defaults, etc. — see [`../peer_confirmed_key_levels/README.md`](../peer_confirmed_key_levels/README.md). Anything not listed under "What changes vs the base strategy" inherits the base default.

Use `configs/config.peer_confirmed_key_levels_1m.yaml` for the shipped full runtime preset.
