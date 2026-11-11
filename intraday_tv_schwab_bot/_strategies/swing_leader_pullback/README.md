# Swing Leader Pullback

A daily-bar swing strategy: it buys a short pullback in a momentum leader at the open and holds it for days. It is the only strategy in the package that carries positions across sessions. **It runs in a dry run only** (see "What is not built").

## The rule

1. **Leaders.** Each morning the screener asks TradingView for the members of the configured indexes (`index_universe`; the shipped preset's S&P 500 and Nasdaq-100, about 510 names; the library's index scan keeps primary listings only, so a member whose primary listing is abroad is left out) with their 1-year and 1-month performance. A member's 12-1 momentum is the price a month ago over the price a year ago, less 1: `(1 + Perf.Y) / (1 + Perf.1M) - 1`. Today's price cancels in it, so the rank does not move with the session. The leaders are the top `leader_fraction` (a tenth) by that momentum, the boundary included. Fewer rankable members than `min_universe_size` is no universe: no leaders. The day's first list stands for the day. The leaders are on the watchlist only while one can be entered: from the screener window's start to the entry window's end, on a session day; outside that, the held positions alone.
2. **Setup.** On a leader's last **completed** session (daily bars, today's forming bar never read): the close above its `trend_sma`-session SMA (200) and Wilder's RSI(`rsi_length`) (2) under `rsi_max` (10). That is a two- or three-day dip inside an uptrend. Two floors are read on the same bars: the last close at least `min_price`, and the 20-session median of close x volume at least `min_dollar_volume`.
3. **Entry.** LONG, in the entry window after the setup's session (09:31-10:00 in the preset), at the 1m frame's price. One entry a session per symbol. When more setups pass than there are free slots, the most oversold goes first (the strategy's score is `rsi_max` less the RSI).
4. **Stop.** `stop_atr_mult` (2) x the `atr_length`-session (14) ATR under the entry price, a plain mean of true ranges. On these names that is about 6% of the price. No target.
5. **Exit.** Near a session's close (from `exit_check_time`, 15:50: that many minutes before the session's close, so an early-close day checks as long before its own): the price above the SMA of the last `exit_sma` (5) closes, today's price standing for today's close; or the `max_hold_sessions`-th (10th) session after the entry's. The entry session counts as session 0 and can exit on its own close.

`rule.py` holds the arithmetic as pure functions; `strategy.py` the entry pass and the exit hook; `screener.py` the leaders.

## Where it comes from

The 2026-11-10 swing study (`/pvr/claude/newstrat`, written up in `.preview/swing_study_2026-11-10.md`). The rule was written down before it was run, and tested on the 494 index members outside the 25 top_tier names, on Yahoo daily bars 2006-2026, costs 5 bps a round trip:

- no stop: 9,032 trades, 68% winners, +0.58% a trade after costs over about 4 sessions; +0.25% a trade over the equal-weight universe on the same days (monthly t 3.7); +0.22% over the same name bought on a random day in the same trend state (t 3.5);
- with this strategy's 2 x ATR stop (every leader, 11,363 trades): 19% stopped, 67% winners, +0.44% a trade, +0.18% over the universe (t 3.2), the worst trade -29%;
- as a 10-slot book (the preset's shape): 12.7% a year 2006-2026 with a worst drawdown of -30%, losing years 2008, 2016, 2018 (-19%) and 2022 (-11%).

**It is a candidate, not a proven edge.** It missed one of its own pass rules (flat in 2015-19); the stock list is today's index members, so every name survived; the idea came from the data; much of the raw profit is the market's own bounce, and it loses in falling markets. The dry run is its test, and at about 40 setups a month that takes a quarter.

`tests/strategies/swing_leader_pullback/test_rule_parity.py` ties the code to the study: driven one session at a time over three names' bars, `rule.py` gives the study's own 102 trades, to the entry and exit session, the kind of exit and the return.

## Where the bot differs from the study

- **The entry price.** The study bought the open print. The bot buys at the 1m price in its entry window, a minute or more after the open.
- **The exit price.** The study sold the close. The bot judges the exit from 15:50 on the price then and sells there.
- **The stop after a gap.** The same: with `execution.extended_hours_enabled: false` (the preset) the stop is judged from 09:30, and a position that opens under it is sold near the open.
- **The universe.** The study ranked today's members through history. The bot ranks the members TradingView lists on the day, which is the point-in-time list.
- **The floors.** The study applied the price and dollar-volume floors before ranking. The bot ranks every member and applies the floors at the entry. Index members almost all pass both.
- **Splits and dividends.** The study used adjusted bars. Schwab's daily bars are split-adjusted, not dividend-adjusted: a dividend shows as a small down gap.

## What the preset sets, and why

`configs/config.swing_leader_pullback.yaml`. Each of these on would close a multi-day position early or move its stop:

- `risk.trade_management_mode: none`: no break-even, profit lock, runner or trail.
- `risk.peak_giveback_enabled: false` and `risk.time_stop_minutes: 0` (the time stop counts wall-clock minutes, nights included).
- every `shared_entry` veto, refinement and score term off, and the divergence-only entry. They read the 1m frame, which holds a bar or two at the entry, and the refinement would pull a daily-ATR stop in to an intraday level.
- every `shared_exit` family off (their sub-switches keep the example's values and do nothing): the exits are the stop and this strategy's close-time check.
- `support_resistance.enabled: false`: nothing the strategy runs reads intraday S/R, and on it would fetch an HTF frame and build a context for every leader.
- `screener_windows` from 09:00: the leaders' 1m and daily history is fetched before the open, not in a burst at the first entry pass.
- `execution.extended_hours_enabled: false`: entries, the stop and the exits in the regular session only.
- `runtime.auto_exit_after_session: false`: the process stays up to carry its positions.
- `risk.max_positions: 10`, `max_notional_per_trade: 5000`, `risk_per_trade_frac_of_notional: 0.06`: ten equal slices, a slice filled whole when its stop, with the entry's slippage allowance (up to 0.2% of the price), is no wider than 6% and cut down in proportion when it is wider. `max_daily_loss: 3500` holds the ten slices' open risk, which counts against it.
- `risk.cooldown_minutes: 480`: with the strategy's own one-entry-a-session rule, a symbol stopped out is not bought again the same day.

The strategy never force-flattens, and a `force_flatten` block in its params is refused at load: the key would do nothing.

## Checked at load

Every knob is checked when the config loads and again when the strategy is built; a bad value refuses to start and names the key: the numbers against their bounds (whole numbers where they count sessions, each at most 1000), `index_universe` as a non-empty list of TradingView index ids (`SYML:<source>;<symbol>`), `exit_check_time` as an HH:MM from 15:00 to 15:59.

## Fail closed

- **Daily bars.** The engine fetches each watchlist symbol's daily history once per day, `daily_history_calendar_days()` back (328 calendar days for a 200-session SMA; `MarketDataStore.daily_history_calendar_days`, which the engine sets from the strategy). The strategy reads the day's cached frame only and never fetches in a pass. An entry is refused when there is no data store, when today's fetch failed (`daily_history_failed`), when the engine has not fetched the symbol yet today (`daily_history_not_fetched`), when there is no completed session (`no_daily_history`), or when the newest bar is not the last completed session (`daily_history_stale(last=...,expected=...)`).
- **The exit.** When the daily closes do not read at the exit check, the bounce cannot be judged that day: it is logged once per symbol per day with the reason, and the position is held unless its hold limit is reached. The stop is the engine's and does not depend on the daily bars.
- **The screener.** A short or unreadable universe yields no candidates, logged when the shortfall changes. A query that raises yields none either, logged with its type, and the next refresh asks again: the engine does not catch a screener's error, so one raised would end the cycle before the carried positions were managed.

## What is not built

- **Nothing is simulated while the bot is down.** A dry-run restart takes the open paper positions back (below), but a stop crossed while it was down exits at the first pass's price, not at the stop, and an exit check that came due is judged at the next one.
- **Live trading is refused at build** (`schwab.dry_run` must be true). The live path for a multi-day hold is built (below, "The live path") but has never met the broker, and the refusal stays until its broker facts are checked by hand and the owner signs off.
- **The whole exit day carries the trade's P&L.** A multi-day trade's realized P&L lands on its exit day, in the risk tally and the day ledger.
- **Earnings are not avoided.** The strategy does not read the event calendar: an entry can come the session before a report and a hold can span one, as the study's trades did.
- **About 50 leaders are streamed from 09:00 to 10:00.** Every candidate joins the watchlist for the entry hour, though only a few have a setup on a given day.
- **A fill beyond the signal's own levels books the default levels.** The entry stage's fallback (`risk.default_stop_pct` / `default_target_pct`) replaces the stop and adds a target when the fill lands past the stop; with a stop 2 daily ATRs away that needs a gap of about 6% between the signal and the fill.

## A dry-run restart

The strategy declares that it holds across sessions (`capabilities.holding` in its manifest: `across_sessions: true`, and `max_sessions_param: max_hold_sessions`, the knob that bounds a hold). For such a strategy, in a dry run, the run's first reconcile takes back the paper positions the earlier process saved (`StartupReconciler.restore_paper_positions`, 2026-11-10): each is tracked as saved, with its entry time, stop and metadata, and entered in the paper account at its entry price, so the hold is still counted from the entry's own session. The log names each restored position and when its row was last saved.

A saved row is restored only when this strategy's dry run wrote it. Refused, each at ERROR with the reason, and dropped at the next save: another strategy's row, a row a live run wrote (or one saved before rows carried that mark), an option position, a row with an exit order working or a stop record that is not a dry run's, a quantity, entry or stop that does not read, and a key already tracked. A store that cannot be read fails the start-up: started empty, the first save would wipe the rows.

Two limits. The preset keeps its state in its own folder (`runtime.log_dir: .logs_swing`, with the saved positions, trades.csv and the day's figures): another bot writing the same file would delete this strategy's saved rows at its first save, and a restart would then find nothing to take back, without a word. And after a restart on a later day the day's P&L counts a restored position from its first mark, so the move while the bot was down is in no day's figure; it is in the trade's result and the run's.

Until 2026-11-10 nothing restored a paper position: the restore modes walk the broker account, which holds no paper share, and the first reconcile's save replaced the saved rows with an empty book. That is still what happens for every strategy that is flat by the close.

## The live path (built, not enabled)

Three things in the engine assumed a position is flat by the close (2026-11-10). The first two now follow the strategy's declaration (`capabilities.holding`) and run for no live strategy while this one refuses `schwab.dry_run: false`; the third holds for every strategy.

- **The stop at the broker is good-till-cancelled.** The disaster stop, a plain `STOP` for the regular session, is sent `GOOD_TILL_CANCEL` instead of `DAY` (`SchwabExecutor.disaster_stop_duration`), so it is live at each 09:30 without the bot and on a day the bot is down. Nothing triggers it between 16:00 and 09:30, and nothing covers the extended sessions. Because it outlives the day, the bot reads the account's orders `runtime.startup_order_lookback_days` back (not from midnight) when it looks for a stop no record tracks: one moved in the Schwab app on an earlier day rests under a new id.
- **A live config must restart the right way.** With `schwab.dry_run: false` the load refuses a config unless `runtime.startup_reconcile_mode` is `restore_hybrid` (the one mode that brings a position back with its entry session, stop and stop order), `runtime.session_reconcile_on_resume` is true (the new-day reconcile settles a position closed in the app overnight), `execution.disaster_stop_enabled` is true, `execution.extended_hours_enabled` is false (an exit outside the regular session cancels the resting stop, and none can be placed again before 09:30), and `runtime.startup_order_lookback_days` is at least `ceil(1.5 x max_hold_sessions) + 5` (20 for the shipped 10) and at most 365. The preset still carries its dry-run values (reconcile mode `ignore`, a 2-day lookback) and would be refused, naming both. At a restart, a position whose saved row no longer matches the account (its stop sold part of it while the bot was down, a split) is not restored at a guess: the attempt fails, the row is kept, entries stay blocked until the two agree, and it is named once at CRITICAL (`CARRIED POSITION UNMANAGED`).
- **What rests overnight is followed.** A sweep of a closed position's leftover orders that is still owed at 16:00 is tried again while the broker takes orders (to 20:00, and from 07:00: once a minute, and ahead of each broker reconcile; if the try ahead of the morning's reconcile fails as well, that day's entries stay blocked until a restart), and the stop an unconfirmed submit left resting overnight is the position's own at the next morning's reconcile, not a foreign order that blocks the day.

- **A stop that filled on a later day while the bot was down is booked.** At the restart a saved position the account no longer holds is booked from its stop's own fill, on the day it filled (`StartupReconciler._book_positions_closed_while_down`): a fill on an earlier day carries the fill's time as the trade's exit time and stays out of today's figures and today's `max_daily_loss` tally; a fill with no time that reads, on a position entered before today, is booked at the restart's time and counted only when it is a loss. This rule is the one that also changes a strategy that is flat by the close: its stop that filled on an earlier day used to be logged and never booked.

Not handled, by choice or by limit:

- **A stop that sold part of the position while the bot was down** needs a hand: the bot blocks entries and manages nothing for that symbol until the account and the saved row agree.
- **Corporate actions.** A split changes the account's quantity and cost, so the saved row stops matching (the case above); the saved stop and levels are not adjusted. Whether Schwab lowers a resting stop on an ex-dividend day or cancels it on a split is unverified.
- **A lost state folder.** Without its saved row a held position is not restored or managed. Its stop still rests at the broker and counts as an order the bot does not own, which blocks entries until it is cleared.
- **A stop moved in the Schwab app is not followed while the bot runs. This must be fixed before a live run.** The bot's record keeps naming the stop it placed, which reads REPLACED; the replacement rests untracked for the rest of the hold. It is adopted before any new stop is placed and cancelled after a close, but when it fills, the fill is not seen: the bot goes on managing shares the account no longer holds, and its next exit would sell them short. (If the bot is down when it fills, the restart reads the saved stop REPLACED, logs it at ERROR, books nothing and prunes the row.) The same holds when the bot's own resize gets no new order id back. This gap is older than the multi-day hold (a day-trading strategy has it for the rest of one session) and a good-till-cancelled stop stretches it to the whole hold. Until it is fixed: do not move the bot's stop in the app.
- **Orders the bot placed and no longer tracks are blocked on, not cancelled.** A position sold by hand while the bot was down leaves its stop resting; a restart forgets a sweep that was still owed. Either stop counts as a working order the bot does not own, which blocks entries until it is cancelled by hand.
- **`startup_reconcile_ignore_symbols` and a carried symbol.** A symbol on the ignore list is left out of the restore, so a position the bot carries in it is dropped at a restart, its saved row pruned and its stop untracked. Keep the list empty, or never list a symbol the strategy may hold.
- **A first live start in the dry run's folder, and a dry run in a live folder.** The saved rows carry a mark for a dry run's rows, but it says who wrote the row last, not where the position came from. A live start leaves a paper row out of the carried-position rule above, prunes it, and names each one the account does not hold at ERROR as a close it cannot book; but a paper row the account happens to match exactly (side, size, cost within 0.3%) is restored as a live position. The other way round, a dry run pointed at a live run's folder drops the live rows (reconcile mode `ignore`) or re-marks them as its own (a restore mode). Until the cut that lets the strategy run live settles this: start live in an empty state folder, and never point a dry run at a live run's folder.
- **A stop that fills over more than one day** is booked whole on the day of its last execution.
- **A hold past the order lookback.** A stop moved in the app earlier than `startup_order_lookback_days` ago is not seen (the bot's own tracked stop is always read by its id).
- **Shares the bot did not open** are skipped at a restart, not blocked: the account is taken to be the bot's.

What the owner checks at the broker before the refusal is lifted: the duration string the API takes for a good-till-cancelled order, how long such a stop lives, whether an ex-dividend day lowers its price, whether a split cancels it, whether a replace keeps it good-till-cancelled, and which time fields a filled order reports (the code reads each execution's `time`, else the order's `closeTime`). Three more came out of the review: whether an order two or three weeks old can still be read by its id (from the second session on, the stop's state is read that way once a minute); how many orders one listing returns and over how long a range (schwabdev says 3000 rows and a year); and whether a held position's average price can change overnight (a restart matches it to the saved entry within 0.3%). The Schwab API answers only between 07:00 and 20:00 ET, so a restart outside those hours waits for it.

## Default windows (the manifest's and the preset's)

- `entry_windows`: `[['09:31', '10:00']]`
- `management_windows`: `[['09:30', '16:00']]`
- `screener_windows`: `[['09:00', '10:00']]`

## Knobs

| Knob | Default | Meaning |
|---|---|---|
| `index_universe` | `['SYML:SP;SPX', 'SYML:NASDAQ;NDX']` | the TradingView indexes whose members are ranked |
| `min_universe_size` | `300` | fewer rankable members than this: no leaders |
| `leader_fraction` | `0.1` | the top share by 12-1 momentum, over 0 and at most 1 |
| `min_price` | `5.0` | the last daily close's floor |
| `min_dollar_volume` | `20000000.0` | the floor on the 20-session median of close x volume |
| `trend_sma` | `200` | sessions of the trend SMA the close must be above |
| `rsi_length` | `2` | Wilder's RSI length |
| `rsi_max` | `10.0` | the RSI must be under it, over 0 and under 100 |
| `atr_length` | `14` | sessions of the ATR the stop hangs on |
| `stop_atr_mult` | `2.0` | the stop's distance under the entry, in ATRs |
| `exit_sma` | `5` | closes in the bounce exit's SMA, today's included |
| `max_hold_sessions` | `10` | the session after the entry's on whose close the hold ends |
| `exit_check_time` | `'15:50'` | the exit check, as a time before a 16:00 close; 15:00 to 15:59 |

## Files in this folder

- `manifest.json`: the plugin's declaration and its knobs' defaults.
- `rule.py`: the rule as pure functions.
- `strategy.py`: the entry pass, the close-time exit, the load checks.
- `screener.py`: the leaders.
