# Preset Config Notes

This directory contains shipped strategy presets such as:

- `config.peer_confirmed_key_levels.yaml`
- `config.peer_confirmed_key_levels_1m.yaml`
- `config.peer_confirmed_trend_continuation.yaml`
- `config.peer_confirmed_htf_pivots.yaml`
- the other `config.<strategy>.yaml` files for the rest of the package

General rules:

- `config.example.yaml` is the canonical full-config template for a custom install and the base the scaffold clones for new presets (see "What `config.example.yaml` carries" below).
- `config.yaml` is the default file used by `main.py` when no explicit config path is supplied.
- Shipped full presets include an explicit `strategies.<name>` block so each preset can run as a portable standalone file.
- At runtime, the selected top-level config file is the single source of truth above manifest/code defaults.

When in doubt:

1. Use `config.example.yaml` for a fresh setup or as the canonical template when maintaining top-level config structure.
2. Use `config.<strategy>.yaml` when you want a shipped tuned preset for a specific strategy.
3. Check `configs/config.<strategy>.yaml` for the shipped runtime preset.

## Shared entry / exit knobs in the presets (2026-09-24)

Since 2026-09-24 every `shared_entry` and `shared_exit` knob is global: one shared entry stage and one exit policy apply it to every strategy (see the root README's `shared_entry` / `shared_exit` sections). Before, each strategy read only the knobs whose helpers it called, and the peer strategies' exit override ignored most of `shared_exit`. Many presets therefore said `true` for a knob their strategy never read.

**Parity first.** Every preset was rewritten to what its strategy EFFECTIVELY ran before the change. A knob the strategy never read is now `false`, and a value the peer override forced replaces what the YAML said. A preset therefore trades as before, except for the three flips below and the fixes listed in `CHANGELOG.md`. The table is the shipped state. `tests/test_preset_parity.py` pins it, so change a row only together with the preset and a reason.

Legend: 1 = on, 0 = off. `tstop` is `risk.time_stop_minutes`, `prox` is `support_resistance.entry_proximity_scoring_enabled`, and the exit columns are `shared_exit.use_structure_exit` / `use_chart_pattern_exit` / `use_candle_pattern_exit`.

| Preset | fvg | dual veto | tech adj | htf div | tech refine | S/R refine | structure | S/R | chart | candle | prox | tstop | struct exit | chart exit | candle exit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `top_tier_adaptive` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | **1** | 0 | 1 | 45 | 1 | 0 | 0 |
| `small_cap_squeeze` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 1 | 30 | 1 | 0 | 0 |
| `peer_confirmed_key_levels` | 1 | 0 | 0 | 1 | 0 | 0 | **1** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `peer_confirmed_key_levels_1m` | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `peer_confirmed_htf_pivots` (also `config.example.yaml`, `config.yaml`) | 1 | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `peer_confirmed_trend_continuation` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| `closing_reversal` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 45 | 1 | 1 | 0 |
| `mean_reversion` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 45 | 1 | 1 | 0 |
| `momentum_close` | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 45 | 1 | 0 | 0 |
| `opening_range_breakout` | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 1 | 45 | 1 | 0 | 0 |
| `microcap_gap_orb` | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 30 | 1 | 0 | 1 |
| `rth_trend_pullback` | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 45 | 1 | 0 | 0 |
| `volatility_squeeze_breakout` | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 45 | 1 | 0 | 0 |
| `pairs_residual` | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 1 | 45 | 1 | 0 | 0 |
| `microcap_pm_breakout` | 1 | **1** | 1 | 1 | 0 | 0 | **1** | 0 | **1** | **1** | 1 | 30 | 1 | 0 | 1 |
| `zero_dte_etf_options` | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 45 | 1 | 0 | 0 |
| `zero_dte_etf_long_options` | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 45 | 1 | 0 | 0 |

Bold cells are the user-decision flips. Some other cells differ from the old YAML because of parity:

- The peer strategies ship a time stop of 0 and their structure / chart / candle exits off; their old exit override ran only the ladder and the technical exits.
- `mean_reversion`, `closing_reversal`, `top_tier_adaptive`, `small_cap_squeeze`, `pairs_residual`, the four peers and both 0DTE strategies ship the dual-divergence veto off, because they never read it.
- `microcap_gap_orb` ships the candle veto off; its old `true` was never read.
- The 0DTE presets ship the technical, HTF-divergence and S/R-proximity terms and both refinements off; the FVG term stays on (both 0DTE strategies also read it for their regime FVG scores). Premium proposals are never refined anyway, and both rank on `strategy_priority_score` with weight 0.

**The three flips (user decisions, on top of parity).** Each switches on a veto the strategy never met before:

- `top_tier_adaptive`: `use_opposing_chart_filter: true`. In the replay the chart veto alone blocked 0 (logged track) to 2 (rebuilt) of 162 entries, both losers, -2.0R. `small_cap_squeeze` stays at parity (off).
- `peer_confirmed_key_levels`: `use_structure_filter: true`, and only that veto; S/R, chart, candle and dual stay off. On the 5m LTF this gate reads, the replay blocked 6 of 13 entries (2 on 1m, 3 in the logged track), and every one with a realized result was a loser. `peer_confirmed_key_levels_1m` stays at parity.
- `microcap_pm_breakout`: `use_structure_filter`, `use_opposing_chart_filter`, `use_opposing_candle_filter` and `use_dual_divergence_veto` all `true`, judged on its 1m frame. Together they removed 4 realized trades worth -3.2R (3 worth -3.6R after the lookahead correction), all from one session, so the evidence is thin, and every replayed entry was at or after 09:58: the flip is unmeasured on the premarket entries its 07:00 window mostly takes. The candle veto abstains when one of the last 3 bars is a single print. `use_sr_filter` stays `false`: its OR clearance blocked 40-60% of this strategy's entries with no edge.

**Off in every preset:**

- `shared_exit.use_sr_loss_exit: false`. The exit is fixed (reachable now), but a full replay fired it on 13 of 235 positions (all top_tier, median -0.72R), where it cost -0.43R per firing against today's management, CI [-0.85, -0.05]. It had been live only for `zero_dte_etf_options`' credit verticals (R on the premium mark), so that is the one preset where turning it off changed behaviour.
- `shared_entry.use_broken_level_guard: false`. It only protected against that exit. The top_tier / small_cap_squeeze presets keep their old thresholds under `shared_entry` (0.0035 / 0.90 and 0.0025 / 0.72).
- `use_divergence_entry_signal` / `use_divergence_exit_signal: false`, and `min_shared_context_score: null`.

**Other preset changes:**

- `peer_confirmed_key_levels` / `_1m` declare `params.require_peer_target_clearance: true`, key_levels' own AND peer-target clearance, which their `use_sr_filter: true` used to run. The global `use_sr_filter` is `false` for both.
- `rth_trend_pullback` and `volatility_squeeze_breakout` ship `risk.same_level_block_minutes: 0`. Their old 30 was inert: the block reads the signal's `entry_price`, which neither strategy stamped until `emit` began stamping it on every price-level signal. Turning it on is a separate go-live decision that needs a beta dry-run. Both 0DTE presets joined them on 2026-09-25 (below).
- Retired strategy params fail at load with their replacement named: `use_sr_veto` (htf_pivots, trend_continuation), `orb_apply_structure_veto` / `orb_apply_sr_veto` (zero_dte_etf_long_options), `orb_bypass_structure_entry` / `orb_bypass_sr_entry` / `reject_entry_near_broken_level` / `broken_level_min_clearance_pct` / `broken_level_min_clearance_atr` (top_tier_adaptive, small_cap_squeeze). So do the renamed keys `shared_entry.use_divergence_filter` / `use_htf_divergence_filter` and the deleted `technical_levels.divergence_block_dual_counter`.

### What `config.example.yaml` carries

`config.example.yaml` runs `peer_confirmed_htf_pivots`. Its `shared_entry` / `shared_exit` sections, `risk.time_stop_minutes` and `support_resistance.entry_proximity_scoring_enabled` are that strategy's parity values, NOT the config dataclass defaults: the time stop, the structure / chart / candle exits and several entry vetoes are off. Three consequences:

- A `--strategy <other>` run against `config.example.yaml` (or `config.yaml`) inherits those values. Take the sections from that strategy's own `config.<strategy>.yaml` instead.
- `scripts/scaffold_strategy_plugin.py` still clones the example for a new preset, but writes the dataclass defaults for those sections and the two knobs, so a new strategy starts from the code defaults rather than htf_pivots parity.
- Every other section is the canonical template, commented; `technical_levels.htf_divergence_max_age_bars` (6) is spelled out there.

## Preset changes from the 2026-09-25 fixes

Two blocks that were on in the 0DTE YAML but had never fired are fixed. Each preset now ships the fixed block off, so it keeps trading as it did. Switching either on is a go-live decision that needs a beta dry-run. `tests/test_preset_parity.py` and `tests/test_zero_dte_shared_entry.py` pin them.

- `zero_dte_etf_options` and `zero_dte_etf_long_options`: `risk.same_level_block_minutes: 0` (was 30).
  - The old block compared an option's premium with the underlying's ATR, so only an exact premium match could trip it, and it matched on the order side.
  - It never fired on an option in the archive: 0 refusals over 2026-05-18..22.
  - The fixed block keys on the underlying's direction and its price at entry. At 30 / 0.3 it would not have fired either.
  - The 20-minute cooldown already covers the underlying in both directions after an exit.
- `zero_dte_etf_options`: `options.credit_pivot_buffer_gate_enabled: false` (was `true`).
  - It read its pivots from the wrong level of the regime result and never fired.
  - Fixed and enabled, it would refuse about 32 of 38 fixture-replay credit entries and 4 of the 8 live ones.
  - `config.example.yaml` and the code default were already `false`.

No other preset value changed. Three fixes and two manifest exemptions change what an existing setting does:

- top_tier's Fix G (`reject_target_beyond_sr`, `target_max_sr_ratio` 0.7 in `top_tier_adaptive`, 0.8 in `small_cap_squeeze`) stays on.
  - In `adaptive_ladder` mode it now passes a first rung on the nearest opposing level. Until 2026-09-25 it refused every laddered trend entry.
  - A rung beyond a nearer, unladdered level is still refused.
- The LTF structure the `use_structure_filter` veto reads confirms no pivot with the still-forming 5m bucket.
- On an inverted reference pair (the reference low above the reference high, after a gap) with the close through both references, the structure bias is now the later of the two breaks instead of always bullish.
  - It reaches everything that reads the structure bias: the `use_structure_filter` veto, `use_structure_exit`, the 0DTE regime's HTF structure-bias veto and the dashboard's structure overlay.
  - It is rare: 17 of 67,891 archived 5m bars flip (all premarket), 2 of 23,329 15m bars flip (both at 09:45), and 0 of 162 top_tier entry verdicts change.
- `use_structure_filter` (on in `top_tier_adaptive` and `small_cap_squeeze`) no longer reaches their `range`, `pullback` and `sr_scalp` regimes. Both manifests exempt them (`capabilities.shared_entry.exemptions`, a user decision). It is inert in `small_cap_squeeze`, whose preset runs none of the three.
- `use_sr_filter` (on in both) no longer reaches top_tier's `vwap_reclaim`. top_tier's manifest exempts it (a user decision); `small_cap_squeeze` keeps the veto on vwap_reclaim.
- Neither exemption switches a gate on or off, and no YAML value moved. The evidence is in the top_tier README, section 6.
