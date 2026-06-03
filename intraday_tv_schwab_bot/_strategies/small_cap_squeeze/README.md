# small_cap_squeeze

A **long-only, multi-regime small-cap "squeeze" strategy**. It hunts low-float
stocks that are already running premarket on heavy volume — names that are
squeezing or setting up to squeeze — and trades them with the
`top_tier_adaptive` multi-regime engine, taking quick scalps or riding the big
moves.

## How it works

### 1. Dynamic screener universe (no fixed list)
Unlike `top_tier_adaptive` (a fixed mega-cap list), this strategy has **no
`tradable` list** — it screens TradingView for a fresh universe. With the
shipped `watchlist_mode: premarket_lock_rth_live`, premarket gappers are
**sticky** (locked once they qualify, so the pre-open universe doesn't churn);
at the 09:30 open it switches to a live RTH re-screen **unioned** with the
locked premarket names (kept warm for a VWAP reclaim), capped to
`tradingview.max_candidates` by activity. Set `watchlist_mode: none` to
re-screen every cycle with no lock. Filters (long bias, ranked by gap% × clipped RVOL):

| Filter | Value | TV field |
|---|---|---|
| Price | $2 – $20 | `close` (→ `premarket_close` pre-RTH) |
| **Float** | **400K – 20M shares** | `float_shares_outstanding_current` |
| Relative volume | ≥ 2.0 | `relative_volume_10d_calc` |
| Change from open | ≥ 5% | `change_from_open` (→ `premarket_change` pre-RTH = premarket gap from prior close) |
| Volume | ≥ 5M | `volume` (→ `premarket_volume` pre-RTH) |

The three canonical fields (`close` / `change_from_open` / `volume`)
auto-resolve to their **premarket** variants before 09:30 ET, so the same
thresholds mean *premarket* close/change/volume during the 08:05–09:30 window
and *RTH* values after the open. Low **float** is the squeeze fuel.

### 2. Regimes
Narrowed (2026-06-02, after two dry-runs) to the **momentum-continuation** set:
**trend, momentum**, plus the opt-in **vwap_reclaim** regime
(`enable_vwap_reclaim_regime: true`) — a long re-entry when price flushes below
session VWAP and reclaims it on a volume pop (the squeeze re-igniting), the entry
that trend (needs `close>VWAP` *and* `ema9>ema20`) and momentum (needs a new
N-bar high) miss. **pullback, range, vol_squeeze, ORB, and sr_scalp are off**
(`disable_*_regime`) — pullback bled on both dry-runs, and the mean-reversion /
breakout regimes don't fit a squeeze-continuation thesis. With ORB disabled the
opening-range carve-out is removed too, so the open trades the continuation mix
continuously — entries run **08:05 → 11:50** with no gap.

### 3. What's removed vs top_tier_adaptive
- **No index/ETF confirmation** (`require_index_confirmation: false`, empty `index_symbols`).
- **No sector support** — empty `sector_groups` / `sector_index_map`, so the sector concentration guard no-ops.
- **No relative-strength gate** (`relative_strength_block_threshold_pct: 0`).
- **No HTF-structure gating** (`require_htf_bias_alignment: false`) — squeezes break prior structure.
- **Long only** (`risk.allow_short: false`).

### 4. Timeframe + the key squeeze tuning
- **1m LTF, native indicators** (`ltf_indicator_span_scale: 1` → 9/20 EMA, 14 ATR) — responsive, with tight ATR-based stops that suit fast scalps. **Raise `ltf_indicator_span_scale` toward 3–5 to smooth signals and widen stops** (primary tuning knob).
- **A squeezer is *always* extended** (high %B, far above VWAP, big bars), so the mega-cap "don't chase extension" gates are **off**: `reject_stretched_entries`, `entry_exhaustion_filter_enabled`, `reject_oversized_entry_bar`, `reject_tech_bias_contradiction`. Leaving them on would block nearly every squeeze entry. The SR / broken-level / target-beyond-SR gates stay on (those keep you from buying straight into resistance).
- **Hybrid scalp + runner management**: move to break-even early (`adaptive_breakeven_rr: 0.6`) to lock the scalp, scale out at S/R rungs via the adaptive ladder, and trail the remaining runner with peak-giveback (loosened for high-conviction days so big moves get room).

### 5. Extended hours
`equity_session_indicator_window: extended` + `extended_hours_tradable_all: true`
— every screened symbol is eligible for premarket entries (the universe is
dynamic, so a hand-listed sublist can't enumerate it). Premarket orders require
`execution.extended_hours_enabled: true`.

## Architecture
`SmallCapSqueezeStrategy` is a thin subclass of `TopTierAdaptiveStrategy` that
only sets `strategy_name` — all behavior is the shared engine, driven by config.
The two backward-compatible base-engine flags it depends on
(`disable_orb_regime`, `extended_hours_tradable_all`) default off, so
`top_tier_adaptive` is unchanged. The screener (`screener.py`) is the
microcap-style universe screener with this strategy's filters.

## Tuning
The shipped params are a **starting point** for elevated small-cap volatility,
not a backtested optimum. Dial in over a beta dry-run — most likely levers:
`ltf_indicator_span_scale` (signal speed + stop width), `risk.default_stop_pct`
and `stop_buffer_atr_mult` (stop room), the `adaptive_*` / `peak_giveback_*`
management (scalp-vs-runner balance), and the screener thresholds
(`min_change_from_open`, `min_rvol`, `min_volume`, float band) to widen or
tighten the universe.

Run with: `--config configs/config.small_cap_squeeze.yaml`.
