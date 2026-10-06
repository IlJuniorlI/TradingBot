# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The stream subscribes LEVELONE_EQUITIES beside CHART_EQUITY and keeps
  a quote book per symbol (`runtime.stream_quotes`, on); nothing reads the
  books yet.** *2026-10-06* — every quote is REST, refreshed once older
  than `runtime.quote_cache_seconds` (6 s): one batch request per refresh
  (2,966 on 2026-10-01, 2,907 on 10-02), each holding schwabdev's one
  request lock about 0.2 s, and at the perf cut's 2.5-4 s passes only one
  pass in two or three refreshes, so management reads a quote up to 6 s old
  on the others. This lays the stream side of the L1 stream-quotes cut; the
  quotes are served from it in a later change.
  - The subscription: `MarketDataStore.start_streaming(symbols, *,
    stream_quote_symbols)` keeps CHART_EQUITY on the streamable equities of
    `symbols` (the watchlist, unchanged) and subscribes those of
    `stream_quote_symbols` to LEVELONE_EQUITIES (`_subscribe_stream_quotes`):
    the watchlist and every held non-option position's symbol
    (`IntradayBot._stream_quote_symbols`), so a position a manifest's
    watchlist leaves out still streams. SUBS when nothing is subscribed (it
    replaces whatever Schwab kept), else ADD and UNSUBS for the difference,
    in one send; fields 0-3, 8, 12, 15, 17, 18, 25, 33, 34 and 42. The change
    is committed to the books before the send, so the snapshot Schwab sends
    after a SUBS or ADD lands in the symbol's new book and a removed symbol's
    book goes at once, and a failed send reverts it: it shares the stream
    sends' back-off (no send for 60 s after a failure, nor while the stream
    is not active; one WARNING with the error's type per run of failures).
    With no streamable equity in `symbols`, `start_streaming` returns before
    either subscription, as it did before for CHART_EQUITY.
  - Both services' keys are in schwabdev's `subscriptions` before the stream
    starts: at each LOGIN response schwabdev replays the record, iterating
    it across awaits (`stream.py:95-105`), and the first LEVELONE_EQUITIES
    send of a stream, recorded behind a slow pass, would have added a key
    mid-iteration ("dictionary changed size during iteration": a reconnect).
    An empty service sends nothing. Pinned against schwabdev 4.0.0.
  - The books (`stream_quotes.StreamQuoteState`, new, market-data layer):
    the stream thread merges each message into one book per subscribed
    symbol under the books' own `threading.Lock`, never the store's; no log
    call runs under it (the lines are emitted after it is released), and an
    engine-thread acquire waits 0.5 s at most, after which the store replaces
    the books with an empty state (ERROR) and the next pass subscribes
    afresh. A book is complete when bid, ask, last and mark have arrived
    since its subscription in the current epoch and Schwab marked it
    `"delayed": false`; it is fresh while it is complete and a
    LEVELONE_EQUITIES data message arrived within the reader's limit
    (heartbeats, responses and CHART_EQUITY bars do not count). Each ADMIN
    LOGIN response starts an epoch and empties every book; an UNSUBS, a stop
    and the symbol-state prune empty theirs; `"delayed": true` empties its
    symbol's, and so does an item the book cannot take (a non-finite or
    negative price, a non-integer quote time, a wrong type), until its
    fields arrive again; data that names no symbol empties every book. A
    read copies the fresh books under the lock.
  - The receiver never raises into schwabdev, which would tear the websocket
    down and reconnect (`stream.py:133-136`), CHART_EQUITY with it: a
    failure past the per-item checks drops every book and logs an ERROR with
    its type, and a failing log handler is swallowed.
  - Logs (`intraday_tv_schwab_bot.stream_quotes`): `Schwab stream login:
    epoch N code=... msg=...` at each connection; `Schwab stream response
    service=... command=... code=... msg=...` for every response, at WARNING
    when the code is not 0 or 26-29 or not an integer; `Schwab stream
    notice: ...` (WARNING) for a notice that is not a heartbeat; `Stream
    quotes: first LEVELONE_EQUITIES item of epoch N: {...}`; `Stream quotes:
    first complete LEVELONE_EQUITIES book for SYM (epoch N)`; WARNINGs for a
    delayed symbol and a rejected item (once per symbol and epoch, DEBUG
    after) and for data that names no symbol.
  - `runtime.stream_quotes` (new; `true` in every preset, right after
    `quote_cache_seconds`, and by default): `true` or `false`, checked at
    load; `false` subscribes nothing.
  - Identical: the 10-01 09:40-10:40 stepped replay (top_tier, a page open)
    and the peer_confirmed_key_levels 05-04 09:40-10:40 window, no stream
    quote data, against 2d70ab2 in every category (the harness's stream
    records the SUBS; nothing reads the books).
  - README: `stream_quotes` and the runtime table.
  - Tests: `tests/market_data/test_stream_quotes.py` (new: the subscription,
    its revert and shared back-off, the service keys, the books, the epoch,
    liveness, rejected, delayed and unattributable data, the receiver's
    isolation and lines, stop, prune, the lock's timeout and its
    discipline, a two-thread read under churn, and schwabdev 4.0.0's replay
    pinned with a reconnect it causes and the store's send that does not),
    `tests/composition/test_cycle_symbol_maps.py` (the held equity streamed
    beside the watchlist), `tests/domain/test_config_validation.py` and
    `tests/guards/test_preset_parity.py` (the switch);
    `tests/support/brokers.py`'s fake stream records schwabdev's
    subscriptions and its fake streamer can hold a replay. 77 mutants, all
    killed, each by its named test.

- **Every engine pass and every management pass is on the record:
  CYCLE_TIMING, POSITION_MARK, the pass before on EXIT_CONTEXT and
  POSITION_ADJUSTMENT, and a live exit's missed limits as a number.**
  *2026-09-28* — logging only; no decision changes, live or dry run.
  Studies B (O6) and F (stage 0) had to rebuild the management cadence and
  the engine's previous look at a position from 1m bars and from log lines
  that appear in only some phases; a top_tier pass takes about 24 s, which
  sets how far past a level an exit is seen.
  - **CYCLE_TIMING**, one per pass of the engine loop
    (`IntradayBot._run_cycles`), light passes included, at DEBUG (the log
    file, not the console): `start`, `total_s` and a `<phase>_s` for each
    phase that took a millisecond or more, in the order they run:
    `reconcile`, `screener` (with the cycle gate), `watchlist`, `history`,
    `daily_history`, `htf_refresh` (the HTF refresh points, added up),
    `stream`, `frame`, `sr`, `contexts`, `warmup`, `quotes`
    (with the account marks), `manage` (the entry-order settle and
    `manage_positions`, a live exit's re-sends included), `entries`,
    `publish`, `error`, `housekeeping` and `sleep`; the phases are
    contiguous and add up to `total_s`. It also carries `watchlist`,
    `managed`, `positions` (those management ran for) and `manage_gap_s`,
    the seconds between two management passes. A pass whose step raises
    names the phase (`failed_phase`) and times its error path; a pass the
    auto-exit or a stop signal ends is recorded too (the loop body is in a
    `try` / `finally`). `step()` times its phases on the pass's
    `_CycleTimer`.
  - **POSITION_MARK** (`runtime.log_position_marks`, on in every preset,
    the local `configs/config.yaml` included, and by default): one per held
    position per management pass that read a price, at DEBUG: `at` (when
    the pass read it), `mark`, `gap_s`, `stop` and `target` coming into the
    pass, and the quote's `bid`, `ask`, `last`, `source` and `price_at`
    (absent for the 0DTE strategies' own marks). Checked at load: `true` or
    `false` (the runtime section's switches).
  - **EXIT_CONTEXT and POSITION_ADJUSTMENT** carry `managed_at` (the pass
    that decided them), `prev_managed_at` and `prev_mark_price` (the pass
    before it and the mark it read) and `managed_gap_s`. An exit the broker
    filled (a bracket child, the disaster stop, a working exit order)
    carries the last pass before its booking and no `managed_at`; a
    position's first pass has no pass before. `PositionManager` keeps the
    last pass per position in memory (`_looks`, tied to the position's entry
    time, dropped with it), not in the position store, so a restart starts
    over and the store is not rewritten every pass.
  - EXIT_CONTEXT names the level the exit is on: `exit_level` and
    `exit_level_kind` (`stop`, `target`, `peak_giveback_floor`,
    `touch_hold_guard`, `broker_stop`, `broker_target`, `disaster_stop`;
    `position_metrics.exit_level`). A broker child's level is the price its
    record says it rested at, the static disaster stop's included. The peak
    giveback's three reason codes are named in `position_metrics`
    (`PEAK_GIVEBACK`, `_HIGH_CONVICTION`, `_LOW_TIER`), which
    `TradeManager.update_position` emits.
  - Each attempt of a live LIMIT exit carries `exit_limits_missed` on
    EXIT_CONTEXT: how many of its limits missed in the pass
    (`OrderResult.exit_limits_missed`, set by the re-send loop; 0 when the
    first order settled it; a rejection is not a miss). An order the
    attempt left working keeps the count on its `working_exit_order`
    record, and the EXIT_CONTEXT of the fill a later pass books from it
    carries it too (`_book_broker_exit`'s `exit_limits_missed`). The result
    message's `;exit_limits_missed=<n>` suffix stays (the settle reads the
    prefix only). A dry run, a MARKET exit and an option's close carry
    none, so no dry-run record changes.
  - The session archive copies CYCLE_TIMING and POSITION_MARK into
    `events.jsonl` (`_STRUCTURED_PREFIXES`). The existing lines and records
    are unchanged but for the added fields.
  - Log volume on the archived days (`review_scratch/liveprep/
    LP3_instrumentation_tools/log_volume.py`: the passes are the quote
    batches inside their span and inferred from the throttled markers
    outside it; the line sizes are the patched code's own, from the real
    loop on 2026-09-24's bars): CYCLE_TIMING about 390 bytes on a full pass
    and 245 on a light one, a median 1.7 MB a day on the 18 top_tier days
    (0.4-2.8 MB, 7.4% of the log), 3.3-5.4 MB on the small_cap and 0DTE
    days (whose 3-5 s loops write logs of 2-6 MB); POSITION_MARK about 325
    bytes, a median 0.1 MB a day on top_tier (0-1.3 MB), up to 2.0 MB on a
    0DTE day; the new EXIT_CONTEXT and POSITION_ADJUSTMENT fields under 14
    KB a day. A record per light pass is kept (they are DEBUG and
    file-only); logging only the managed or entry passes would cut
    top_tier's CYCLE_TIMING to about 0.4 MB a day and lose the premarket
    and after-close cadence.
  - Tests: `tests/composition/test_cycle_timing.py` (new: the real loop
    over a real bot, a record per pass, the phases adding up, a failed
    pass, a stop signal mid-step, the auto-exit's pass; the timer),
    `tests/runtime/test_management_instrumentation.py` (new: the pass
    before on each record, a pass without a price, a bracket fill, a
    disaster stop's fill, a new position under the same key, the marks and
    the knob, the level and the slippage per exit, none for a working exit
    the broker filled with no price, the broker child codes pinned against
    the booking's), `tests/runtime/test_exit_reprice.py` (the count per
    outcome, on the records, the settle's record of a tracked order, and
    absent from a MARKET or dry-run exit's), `tests/reporting/test_session_archive.py` (the two
    DEBUG records reach `events.jsonl`), `tests/guards/test_preset_parity.py`
    (the knob on in every preset and the local config). The engine shells
    in `tests/composition/test_engine_shutdown.py` and
    `tests/runtime/test_startup_reconciler.py` carry an `AuditLogger`, and
    the recording audits in `tests/support/brokers.py` and
    `tests/runtime/test_partial_exit.py` take the `level` a record is
    logged at.

- **A live LIMIT exit that misses is re-sent at a fresh quote in the same
  management pass, within one time budget per pass:
  `execution.exit_live_reprice_attempts` (2), `exit_live_reprice_step_frac`
  (0.5), `exit_live_reprice_max_seconds` (12) and `exit_live_market_fallback`
  (off).** *2026-09-28* — study B's O1. Every engine exit outside the
  regular session, and inside it unless `market_exit_regular_hours`, is a
  marketable LIMIT at the bid less the entry buffer (the ask plus it, for a
  cover). Live, one still unfilled after `entry_live_fill_timeout_seconds`
  was cancelled and the attempt ended: the next order went out on the next
  management cycle, a median 24.4 s later on top_tier days (3.8 s on
  small_cap days), provided the exit still fired. A dry run cannot show
  this: it fills at the bid of the quote the exit read.
  - `SchwabExecutor._submit_live_equity_exit_with_reprice`: an order the
    broker confirmed CANCELED with nothing filled (`live_unfilled_canceled`,
    now `execution.LIVE_UNFILLED_CANCELED`) is followed by another, priced
    off the quote read then, its spread buffer (1 + n x
    `exit_live_reprice_step_frac`) times the first's, up to
    `exit_live_reprice_attempts` re-sends. None goes out once the equity
    session is no longer the one the exit was priced for (a NORMAL order is
    not re-sent past 16:00, nor an AM one past 09:25). Any other outcome
    ends the call: a fill of any size (a partial books and the next cycle
    exits the rest), a refused submit, a missing order id, a cancel the
    broker did not confirm (tracked as a working exit), and an order the
    broker `REJECTED` or `EXPIRED` after accepting it or that was `REPLACED`
    at the broker (`;stopped=status:<STATUS>`, below). So no second exit
    order for the same shares goes out while one may still fill, except
    after a submit whose outcome is unknown: a re-send's POST that times out
    raises out of the call as the single exit's did, with the order perhaps
    live and untracked, which the queued shared unknown-outcome path is to
    cover. One answered 5xx (or with a status that is neither 2xx nor 4xx)
    ends the call as `status=<code>`, and
    `broker_payloads.order_result_needs_broker_recheck` reads it as one that
    may have reached the broker, as the disaster stop's own submit does:
    only a 4xx is a refusal (2026-09-29, the final review's probe). As first
    cut every `status=` read as never reached, so the disaster stop the exit
    had cancelled went back in the same pass beside an exit that had
    landed and filled (stops resting 100 against 0 held); now it goes back
    on the next pass that decides no exit, as a miss, until the shared
    lookup covers exits.
  - Only `CANCELED` is followed (2026-09-29, a verifier's finding). The
    single-order submit read every terminal status the cancel's check saw as
    `live_unfilled_canceled`, a 400 on the cancel of an order already dead
    included, so an order the broker accepted and then `REJECTED` was re-sent
    twice more, each counted as a missed limit, and one `REPLACED` in the
    app was followed by two more exit orders for the same shares while its
    replacement worked. Now `_submit_live_single_order_with_poll` labels a
    dead order by its status: `LIVE_UNFILLED_REJECTED` and
    `LIVE_UNFILLED_EXPIRED`, logged at ERROR, end the call; a REPLACED
    order's result names its replacement when the broker's payload does
    (`broker_payloads.order_replacement_id`, `replacingOrderCollection`,
    `LIVE_ORDER_REPLACED`, `may_still_be_working`), so the position manager
    tracks that order in its place (booked from its own fills), and logs
    `ORDER REPLACED UNTRACKED` at CRITICAL when it does not; an order
    REPLACED between its poll and the bot's cancel, after a partial fill or
    none, is read the same way. Options' closes get the same labels.
  - An exit order REPLACED at the broker is followed to its replacement
    wherever it is read (2026-09-29, the verifier's second probe). One whose
    payload names none is looked up among the day's orders on the next pass
    (`PositionManager._follow_replaced_exit`,
    `broker_payloads.replacement_order`: the one exit order of its kind on
    the symbol for the side entered after it) and tracked in its place;
    while none is found (none listed, several that fit, or the orders
    unreadable) the position is held, no exit or re-protect sent for it and
    the others managed as usual, and every
    `disaster_stop_escalation_attempts`-th pass logs `ORDER REPLACED
    UNTRACKED` at CRITICAL until the orders show it. A tracked exit order
    found REPLACED on a later pass is followed the same way (the one its
    payload names, else looked up), and a restore counts the shares of a
    REPLACED working exit as still covered. Until then the pass after read
    the replaced original as settled, dropped it and re-protected the
    position in full beside the replacement, and a re-decided exit could go
    out beside it too; and a partial fill before an unnamed replace was
    booked twice (the original was tracked with nothing booked).
  - Each live order of an exit goes to the position manager before it is
    polled (`close_position`'s and `submit_equity_exit`'s required
    keyword-only `on_order_sent`), which records it as the position's
    `working_exit_order` and saves it (2026-09-29): a stop signal (it is not
    an Exception) or an error during the poll leaves the order tracked for
    the next cycle, or a restart, to settle. Until then a KeyboardInterrupt
    in a re-send's poll left it working and untracked, and a restart rested
    a full-size disaster stop beside it. A result whose last order the
    broker confirmed dead with nothing filled
    (`execution.order_result_left_nothing_live`) is no longer tracked as a
    working exit: the protection the exit took down goes back in the same
    pass, not on the next.
  - One budget per management pass. `PositionManager.manage_positions`
    takes one deadline as it starts
    (`SchwabExecutor.exit_reprice_deadline`: `exit_live_reprice_max_seconds`
    from then) and hands it to every exit it sends (`close_position` and
    `submit_equity_exit` take a required keyword-only `reprice_deadline`); no
    re-send, nor the fallback, goes out past it, whichever position it is
    for. Each exit's first order always goes out. A budget per exit let
    positions that missed together run theirs one after another: with four
    whose limits all miss, the fourth's first order went out 23-35 s into
    the pass (7.6-11 s with one order per exit). With one per pass it goes
    out at 15.4 / 17.9 / 19.0 s at a 0.1 / 0.3 / 0.5 s broker round trip,
    and the pass ends at 17.8-22.0 s (17.5-24.5 s with the fallback on),
    against 30.8-46.0 s (40.0-56.0 s) with a budget per exit (the live-prep
    critic's four-position probe on the scripted broker, re-run on this
    code).
  - `exit_live_market_fallback` (regular session only): once the limits
    have run out, or a re-send could not be priced for want of a fresh
    quote, a MARKET order goes out under the same budget and session
    checks, left working and tracked if it does not fill in the poll window,
    as `market_exit_regular_hours`' MARKET exits are. Off in every preset: it
    trades a limit's no-fill for a MARKET order's slippage, a go-live
    decision per strategy.
  - The result's message says what happened, in the `Exit attempt` log line
    and EXIT_CONTEXT's `result_message`: `;exit_limits_missed=<n>` once a
    limit has missed, then `;exit_market_fallback`, or
    `;stopped=attempts|time_budget|session:<now>|missing_or_stale_quotes`
    when nothing filled. The prefix is the last order's own, so the
    position manager settles the result exactly as before. Each re-send and
    the fallback log a TRADEFLOW line (`Exit SELL ABCD qty=900: limit 9.9700
    unfilled and cancelled (order 7001) at 2.6s; re-sending at 9.8500 (1 of
    2)`).
  - Measured on a scripted broker for one exit (the presets' 2 s fill
    timeout and 0.25 s poll; `LP4_exit_reprice_start_tools/live_exit_timing.py`):
    at a 0.1 / 0.3 / 0.5 s broker round trip the second order goes out
    2.6 / 3.7 / 4.0 s after the first (before: the next cycle), three missed
    limits end the call at 7.7 / 10.8 / 11.5 s, and the fallback MARKET order
    goes out at 7.8 / 11.1 / 12.0 s. Over that wait a top_tier exit's price
    moves a median 0.064-0.076R (one standard deviation) against 0.189R over
    the 24.4 s cycle; small_cap's 3.8 s cycle is about one re-send already
    (study F's diffusion model on 108 of study B's regular-hours level exits,
    those with a 1m ATR; a floor, since a miss is not a random moment;
    `miss_exposure.py`).
  - Dry runs are unchanged: 26,400 dry-run exits (5 presets, 10 times across
    every session and blackout, extended hours on and off, both intents, 8
    quote shapes, the 3 snapshot forms, refused quantities and symbols,
    through `submit_equity_exit` and `close_position`) are byte-identical to
    the previous code under five knob settings, and none reaches the broker.
  - The entry re-price loop shares the re-pricing helper
    (`_build_repriced_equity_request` now takes the buffer multiple) and
    keeps its own knobs, the 0.05 floor on its step included. Options exits
    are unchanged (one order a pass; they ignore the deadline).
  - Checked at load: the attempts an integer >= 0, the step a finite number
    >= 0, the budget a finite number in (0, 60] (the pass waits on it), the
    fallback true or false, and the fallback on with
    `market_exit_regular_hours: true` is refused (every regular-session exit
    is a MARKET order already, so it could never act). Every preset declares
    the four at the code defaults (the local `configs/config.yaml` and the
    two microcap presets included); `tests/guards/test_preset_parity.py`
    pins them.
  - Tests: `tests/runtime/test_exit_reprice.py` (new; `TestOneDeadlinePerPass`
    runs several positions through `manage_positions`; an order the broker
    rejects or expires after accepting it, one replaced in the app (before
    its first status read or its cancel, named or looked up, found or held,
    after a partial fill), the hand-over before each poll, a stop signal in
    a re-send's poll, an attempt that left nothing live),
    `tests/runtime/test_partial_exit.py` (the deadline reaches the equity
    exit), `tests/domain/test_config_validation.py`,
    `tests/guards/test_preset_parity.py`; every stand-in executor that
    `manage_positions` drives answers `exit_reprice_deadline`
    (`tests/support/brokers.py::_executor_stub`), and every stand-in
    `close_position` takes the keyword.

- **A static disaster stop rests at the broker for each live equity position
  (`execution.disaster_stop_enabled`, on by default and in every equity
  preset, with `disaster_stop_r: 1.0`, `disaster_stop_min_pct: 0.0025` and
  `disaster_stop_escalation_attempts: 3`).** *2026-09-28* — study B's O5.
  Brackets are off in every preset, so until now nothing rested at the
  broker, and a position the bot stopped managing (a crash, a hang, a lost
  connection) had no stop at all.
  - One plain `STOP` per position (`DAY`, regular session), resting
    max(`disaster_stop_r` x initial R, `disaster_stop_min_pct` x entry)
    beyond the initial stop and rounded a tick away from the market; a LONG's
    is at least $0.01. It is priced off the fill and the stop the position
    keeps (the default-distance one after a fill through the signal's),
    stamped on the position as `disaster_stop_price` (EXIT_CONTEXT carries
    it), and never moved as the engine's stop ratchets. The floor keeps a
    trade whose initial risk is inside one minute's noise from resting it
    there too.
  - The engine keeps its own stop and every exit: it checks its stop every
    cycle whatever rests at the broker (see **Fixed**). Every engine exit
    cancels the disaster stop first (the existing cancel before an exit),
    and books what it sold before the cancel landed, except a tracked one
    the user moved in the app: Schwab replaces it under a new id, the cancel
    reads the REPLACED original as down, and the exit goes out beside the
    replacement, so a flush can fill both (queued before any live flip, in
    H_CHECKLIST; after a full close the sweep cancels the replacement).
  - It is kept as the position's bracket record with `sync_mode: disaster`, a
    flavour of its own (`static` is refused with the adaptive modes, whose
    stop ratchets). So the fill reconcile books a filled one as a
    `disaster_stop` exit; a scale-out's remainder gets a fresh one at the
    same price; one that died at the broker is re-placed at once, and one
    that dies again, or that the broker `REJECTED`, is owed again a retry
    interval later and counted (below); and the restore adopts one still
    resting (resized to what is held, with what it sold before the snapshot
    booked on it, below), places one at the saved price when none rests, and
    never counts it as a foreign order. With it on, a live restore waits for
    the working-order list, as in bracket mode. A dry run's restore adopts
    no stop of the account, which stays a foreign order (it holds entries
    in the restore modes), and restores what is held when the list cannot
    be read, as with the disaster stop off (2026-09-29, the final review's
    probe: the paper position took the user's stop as its own, lifting
    `working_orders_present`, and an unread list restored nothing).
  - It goes out right after the entry fills and the position is saved.
    Outside the regular session nothing is sent (Schwab rejects a `STOP`
    there), nor written or saved each cycle (a new position gets a
    `pending_session` record in memory), and the first cycle after 09:30
    places it. A submit the broker refused (a 4xx) is tried again a minute
    later.
  - A submit whose outcome is unknown is never re-sent blind: a transport
    failure, a response that timed out (`schwab_api.
    SCHWAB_WRITE_UNKNOWN_OUTCOME`: schwabdev never retries a POST, so a
    `place_order` whose response times out raises `requests`' `ReadTimeout`,
    which the read-side `SCHWAB_TRANSPORT_ERRORS` does not name), a 5xx (a
    gateway error on a POST says nothing of whether the order landed:
    schwabdev retries GET, PUT and DELETE only; until 2026-09-29 a 5xx read
    as refused, and the retry placed a second stop beside one that had
    landed), a status that is neither 2xx nor 4xx, that is not a number
    (until 2026-09-29 `schwab_api.response_ok` raised ValueError on one, out
    of the placement) or that the response does not carry, or an accepted
    order without its id. A minute later the day's orders are read, whatever
    their status (`broker_payloads.extract_orders`,
    `SchwabExecutor.fetch_orders` / `todays_orders`, from a minute before the
    submit on: `sent_at`, which every unconfirmed record carries and a miss
    never moves; `sent_exit_stop`), for the exit `STOP` at exactly the price
    and quantity sent (the quantity sent, whatever the position holds since).
    What it filled, in part or in full, and the record has not booked, is
    booked as a `disaster_stop` exit at the broker's price (the level sent
    when it gives none), so the position is closed or reduced as the fill
    reconcile would; one in any status but a terminal one is adopted
    (resized to what is held); one REPLACED at the broker (moved in the app)
    is followed to its replacement (the one its payload names, else the one
    exit stop entered after it), and while that cannot be found none is
    placed, as a miss; one that died, or none, lets one be placed
    (`PositionManager._look_up_unconfirmed_stop`). A status is read failing
    closed (`broker_payloads.order_status_class`): only FILLED, CANCELED,
    REJECTED, EXPIRED and REPLACED end an order, and any other one,
    PENDING_CANCEL, PENDING_REPLACE, NEW, AWAITING_RELEASE_TIME, UNKNOWN, an
    unknown or a missing one, may still rest. Until 2026-09-29 only working
    orders were read: a stop that landed and filled before the lookup was
    never booked, the bot kept shares the account no longer held, and placed
    a full-size stop for them; and a stop outside the working allowlist
    (PENDING_REPLACE while a user edits it in the app, NEW) or one moved in
    the app read as dead, so a second went in beside it.
    `PositionManager.ensure_disaster_stop` saves the record as `unconfirmed`,
    at the price and quantity about to be sent, before it sends anything, so
    an error anywhere after that leaves a stop that is looked for before
    another goes out. A restore whose protection step raises leaves it
    `unconfirmed` at the saved price for the shares no working exit covers,
    sent at the restart (until 2026-09-29 it had no submit time, so its
    lookup read the day from midnight and could book an earlier position's
    filled stop at the same price and size against the position), and a
    restored `unconfirmed` record adopts the stop at exactly the price and
    quantity it sent, in any status but a terminal one, never merely the
    first exit stop on the symbol (which may be one placed by hand). A
    restore books on the stop it adopts what that stop sold before the
    snapshot (`booked_child_fills`, from the working-order rows): the shares
    it restores are the account's, already net of them; and every adoption
    keeps what the record it adopts from had booked
    (`SchwabExecutor._tracked_protection_ids`). Until 2026-09-29 a stop that
    had filled in part while the bot was down was booked in full when the
    rest filled, and `EXIT OVERFILLED` named a short the account did not
    hold (the final review's probe).
  - Before an exit or a slice, an `unconfirmed` record is looked up at once
    (2026-09-29): its fills are booked and the exit sells only the rest, and
    a stop that may still work is adopted (a moved one's replacement too), so
    the cancel before the exit takes it down first, and a slice's re-protect
    rests one stop for what is left. Orders that cannot be read, or a moved
    stop whose replacement is not found, defer the exit a cycle, counted as
    a miss. Until then the exit went out beside it with no cancel, and a
    stop that filled in the same flush sold the shares twice; a slice left
    the full-size stop resting against what was left. A slice left working
    whose re-protect's submit had an unknown outcome leaves that stop to
    `ensure_disaster_stop`'s lookup when it settles
    (`PositionManager._disaster_stop_unconfirmed`, 2026-09-29, the final
    review's probe: the settle placed a second stop at once without looking
    for the one that landed, whose fills were never booked).
  - Every placement first reads the day's orders
    (`SchwabExecutor.ensure_position_protected`): an exit stop no record
    tracks that may still work on the symbol for the side (in any status but
    a terminal one: one moved in the app, which Schwab replaces under a new
    id, or one placed there) is adopted, resized to what is held and logged,
    instead of placing another beside it; orders that cannot be read place
    nothing (`unprotected`, retried). This replaces
    the lookup's rule that neither adopted nor placed beside a stop it could
    not tell for its own. Until 2026-09-29 a scale-out after the user moved
    the stop in the app placed a stop for the rest beside the moved one: 150
    shares of stops against 50 held. A stop the lookup or this read finds is
    adopted as that read lists it (`broker_payloads.listed_stop`), never
    read again; tracked from then, what it sells is booked by the fill
    reconcile (2026-09-29, the final review's probe: the adoption read the
    orders a second time, a stop that filled in between read as gone, a new
    one was placed for the shares it had sold, and its fill was never
    booked).
  - A stop resting more shares than are held (`qty_mismatch`, an adopted one
    the broker would not resize; `qty_unverified`, one whose size could not
    be read) is owed too: the next cycle resizes it again, then cancels it
    and places one at the held size; one that neither resizes nor cancels is
    a miss. Until then it stayed, larger than the position, never retried,
    cancelled or escalated. So is one left resting fewer shares than are
    held by late entry fills whose resize was not confirmed
    (`PositionManager.disaster_stop_resize_refused`, from
    `EntryGatekeeper._grow_position`): `qty_mismatch` at the size it rests,
    a miss at once, resized again a retry interval later, then cancelled and
    placed at the held size (2026-09-29, the final review's probe: it stayed
    `disaster_stop`, the shares beyond it with no broker stop for the rest
    of the trade, never retried or escalated).
  - Isolated per position at entry: a placement that raises right after an
    entry is logged against that position with its type and traceback
    (`EntryGatekeeper._ensure_disaster_stop_after_entry`), and the entry pass
    goes on; the next management cycle places the stop still owed.
  - Escalated: each attempt that leaves a position without its stop logs on
    its own line (a refused or unknown-outcome submit, an order list that
    cannot be read, an unconfirmed stop moved in the app whose replacement
    is not found, a stop that went down with the position held and no
    exit working, one resting more than is held that neither resizes nor
    cancels, one left resting fewer by late entry fills whose resize is not
    confirmed, a cancel before an exit that cannot be confirmed, which holds
    the exit), and every `disaster_stop_escalation_attempts`-th consecutive
    one logs `DISASTER STOP DEGRADED` at CRITICAL naming the position. The
    count ends when the broker lists the stop working (the fill reconcile),
    not when a submit is accepted. A stop that went down counts: one the
    broker `REJECTED` after accepting it or that died again (placed again a
    retry interval later), and one the cancel before an exit took down when
    the exit did not follow (an exit whose submit raised, or whose order got
    no id; placed again on the next cycle that decides no exit). Until
    2026-09-29 none of these was placed again, and a rejected one logged a
    WARNING only.
  - A broker fill larger than the position (the fill reconcile, the
    unconfirmed lookup, a cancel's fills, a working exit's) is booked as the
    whole position and logged `EXIT OVERFILLED` at CRITICAL naming the extra
    (`PositionManager._held_part_of_fill`, 2026-09-29): a stop the broker
    listed only after a slice had sold beside it leaves the account short
    the rest, untracked. Until then each booking capped it at the position,
    with a WARNING at most.
  - After a full close of a position that had live broker protection, the
    day's orders are read and any exit order that may still work on the
    symbol for its side (any status but a terminal one) is cancelled
    (`PositionManager._sweep_exit_orders`): a stop whose unknown-outcome
    submit landed after all, a replacement a replace left untracked
    (`new_id_unknown`), a stop moved in the app. One that cannot be
    cancelled, or that filled first, and an unconfirmed stop (or its
    replacement) that filled beside the exit, log `EXIT ORDERS LEFT` at
    CRITICAL, each order's fills once. A close whose record was still
    unconfirmed keeps its sweep owed until a retry interval after the
    stop's submit, run again at the start of each pass until then, so a
    stop the broker lists only after the close is still cancelled
    (2026-09-29). A read that fails is owed too (`_run_exit_sweep`): tried
    again at the start of every management pass, and every
    `disaster_stop_escalation_attempts`-th consecutive failure logs `EXIT
    ORDERS LEFT`; while a sweep is owed the symbol takes no entry
    (`exit_sweep_owed`, skip reason `exit_orders_unswept`). Until 2026-09-29
    the sweep read once, cancelled only the working allowlist, and a failed
    read logged an ERROR once and was dropped. The sweep never cancels an
    order an open position tracks (`PositionManager._tracked_order_ids`,
    2026-09-29, the final review's probe: a position an unsettled entry
    order's late fill opened in the symbol while the sweep was owed, which
    the entry hold does not see, had its disaster stop cancelled on every
    pass until the sweep ended). An owed sweep is not saved: a restart
    forgets it. A dry run reads nothing.
  - Measured on the archive (study B's trade table and the archived 1m tapes:
    176 trades on 22 equity days): at 1.0R it would have fired on none of
    them while the engine held the trade (whole 1m bars). Counting every
    print of the entry and exit minutes, it fires on 4 without the floor and
    on 1 with it (ADBE 09-24, in the minute the engine stopped out itself).
    At 0.5R with no floor it fired on AMZN 09-23 (-3.5R), which the floor
    removes. Held to the close with no exit at all, 22 of the trades went 5R
    or more against the entry and 8 went 10R or more; the disaster stop caps
    a trade at about 2R (the median; 9.3R at most, for a trade whose R was
    0.03% of its price).
  - A dry run places nothing, and no dry-run result moves (its restore
    adopts no stop of the account, 2026-09-29, above).
  - Checked at load: the switch is `true` or `false`, `disaster_stop_r` a
    finite number above 0, `disaster_stop_min_pct` a finite number in
    [0, 1] and `disaster_stop_escalation_attempts` an integer >= 1
    (`config._NUMBER_CHECKS["execution"]`). It is refused beside
    `bracket_orders_enabled` (two resting stops on the same shares both sell
    when a flush takes out both), for an options strategy (equity positions
    only; the two 0DTE presets say `false`, and so must any options config,
    since on is the default), and, with `schwab.dry_run: false`, beside
    `runtime.reconcile_on_startup: false` or `startup_reconcile_mode:
    ignore` / `log_only`, which would forget the stops a restart finds
    resting (`config._validate_disaster_stop_restart`; each error names both
    keys). `tests/guards/test_preset_parity.py` pins the presets and the
    local `config.yaml` when it exists.
  - Also: `broker_payloads.collect_protective_fills` takes the stop leg's
    reason (`stop_reason`); `StartupReconciler._resting_stop_for` is
    `broker_payloads.resting_exit_stop`, on top of the new `exit_orders`,
    and returns a stub adopted as listed (`listed_stop`);
    `extract_orders` rows (`extract_working_orders` keeps the ones that may
    still work) carry `stopPrice`, `quantity`, `filledQuantity`, `fillPrice`
    and `replacementId`, and `flatten_order_tree`'s states `replaced_by`;
    `broker_payloads.order_status_class` (with `order_may_be_live`) is the
    one reading of whether an order may still rest, so the startup
    reconcile's working-order snapshot (`fetch_working_orders`: its
    foreign-order check, its `working_orders_present` block and the
    restore's adoption) keeps every status but a terminal one too, where it
    kept an allowlist (a PENDING_CANCEL, NEW or UNKNOWN order was not
    counted); `replacement_order`, `order_entered_at` and
    `order_row_filled_qty` read the rows; `schwab_api.response_ok` reads a
    status that is not a number as a failure;
    `PositionManager.ensure_disaster_stop` takes the cycle's bars (a fill
    the lookup books reads them); `SchwabExecutor.regular_session_open` says
    whether a STOP can rest now; `SchwabExecutor.ensure_position_protected`
    takes the level the protection rests at (`resting_stop_price`; the
    engine's stop for a bracket), and `protective_stop_level` rounds a level
    as a protective order is sent; and the restore's warning when it cannot
    re-establish protection names the error's type.
  - Tests: `tests/runtime/test_disaster_stop.py` (with the 5xx and the
    status answers, a stop that landed and filled in full or in part or
    died, the quantity sent, the record saved before the send, the lookup
    before an exit or a slice, the oversized stop, the stops that went down,
    the moved stop, the premarket cycles, the owed sweep and the entry hold,
    the restore's exact match; each status that is not terminal through the
    lookup, an exit, a placement, the restore and the sweep; an unconfirmed
    stop moved in the app, its replacement found or not; a fill beyond the
    holding; the sweep kept owed through the retry interval; the submit time
    on every unconfirmed record; and from the final review: a stop found
    working that fills before its adoption, a slice's unconfirmed re-protect
    left to the lookup, an exit answered 5xx, a stop left smaller by late
    entry fills, the fills a restore books on the stop it adopts, the sweep
    beside a position opened since, the dry-run restore, the restore's level
    off the initial stop, an adopted entry filled through its stop), the
    timed-out POST and a status that is not a number in
    `tests/foundation/test_schwab_api.py`,
    `tests/domain/test_broker_payloads.py` (every Schwab status and an
    unknown or missing one, the rows of every status, the exact match's
    order, the replacement), and the preset, number and bracket-mode tests
    that now name the switch.

- **`shared_exit.adaptive_ladder_touch_hold` (off in every preset and by
  default) and `adaptive_ladder_touch_hold_timeout_seconds` (45).**
  *2026-09-27* — the adaptive ladder's option to ride past a rung
  (`risk.trade_management_mode: adaptive_ladder`, an equity position with
  ladder rungs). Off, the first rung is a plain take-profit. On:
  - The first price at or through the target holds the position:
    `TradeManager.update_position` leaves the target while the hold lasts.
    The touch belongs to the 1m bar the price was observed in, the quote's
    `fetched_at` (up to `runtime.quote_cache_seconds` before the cycle's
    clock) or the bar whose close it is; the management snapshot now names
    that time (`price_at`).
  - When that bar is delivered it is judged once. A close at or through the
    target at least 55% of the way up its range (down, for a SHORT)
    promotes the rung: the stop moves to the rung less the stop buffer
    (never loosened), the target to the first later rung more than 0.05%
    past that close, or none past the last rung (a runner). The stop buffer
    is the widest of the S/R level buffer (a finite number above 0), a
    quarter of the rung's zone width and 0.05% of the touch price. A price
    already at the new target starts that rung's hold in the same pass. Any
    other close, a bar with no range or an unreadable price included, exits
    at market: `target_weak_close:<rung>`.
  - While it holds, a price the stop buffer back through the rung exits at
    once (`target_hold_guard:<guard>`), and a touch bar still undelivered
    the timeout after it closed exits at market
    (`target_hold_timeout:<rung>`). A bar delivered late is still judged.
  - The three are `risk` exits (EXIT_CONTEXT `exit_reason_family`), each
    its own `per_exit_reason` bucket; a stop or peak-giveback exit on the
    same cycle wins. A decided exit stays on the hold, so an order that
    fails or is deferred is sent again with the same reason, whatever the
    price does next, and the target is never taken instead.
  - Ladder metadata read back from the position store that the hold cannot
    use (the rungs, the active rung index or rung, the price's time) is
    reported as a WARNING and the position keeps its target exit. A stored
    hold is read whole (every field it reads, the times with their zone) or
    dropped with a WARNING, so a restart can never apply half a promotion.
    A ladder pass that raises drops the hold, so its target still fires
    (`manage_positions`); with the knob off, a hold left from a run with it
    on is dropped and logged.
  - There is no index veto. Each touch logs `LADDER_TOUCH symbol= side=
    rung= level= touch= guard= bar= price_at= deadline=` and each verdict
    `LADDER_VERDICT ... verdict= close_pos= guard_hit= outcome= price= stop=
    target= held_s=`, at INFO, for a dry-run A/B.
  - Checked at load: the switch must be `true` or `false` and the timeout a
    finite number in (0, 3600] (`config._NUMBER_CHECKS["shared_exit"]`: past
    about 9.2e9 s it raised in the ladder pass); on, it needs
    `risk.trade_management_mode: adaptive_ladder` (under another mode it
    could never act) and, with brackets on, `execution.bracket_legs:
    stop_only`.
  - `config.example.yaml` and the three `adaptive_ladder` presets declare
    both; `tests/test_preset_parity.py` pins them off in every preset.
  - Replay against the default (2026-09-26, reproduced 2026-09-27; 1m bars
    walked open, low, high, close, fills at the levels, no spread): +1.18R
    over 105 archived laddered trades (9 touched rung 1; per touched trade
    -0.42R to +2.14R, median -0.15R; one runner, DXST 2026-06-02, carries
    it), +0.10R at the 8 recorded target exits, +2.46R on the fixture tapes
    (one touch). Without the in-bar guard: +1.05R and -0.42R. Nine to
    twelve touches cannot separate this from noise: turning it on is a
    dry-run decision.
  - Tests: `tests/test_ladder_touch_hold.py` (new), `tests/test_properties.py`
    (G), `tests/test_position_isolation.py`, `tests/test_preset_parity.py`,
    `tests/test_config_validation.py`, `tests/test_bracket_orders.py`,
    `tests/test_risk_manager.py`.

- **The exit and entry records name the levels an exit turned on.**
  *2026-09-27* — logging only; no decision changes. The giveback, profit
  lock and trail exits could not be told apart in the archive (a profit-lock
  or trail exit is a `stop`), and the ladder's rungs had to be rebuilt from
  the S/R lists (the S1 exit and S2 rung studies, 2026-09-26).
  - EXIT_CONTEXT, every exit: `stop_source`, the management step that last
    moved the stop, `<manager>:<reason>` (`adaptive:profit_lock`,
    `adaptive:trail`, `adaptive:breakeven`, `adaptive:partial_breakeven`,
    `adaptive_ladder:touch_promoted`, `options_ratchet:...`, `sr_flip:...`),
    or `initial`; `stop_r` and `peak_r`, that stop and the trade's best
    price in R from the entry, positive the trade's way. The stop level
    itself is `stop_price`, the peak `highest_price` / `lowest_price`.
    `append_management_adjustment` records `stop_source` in the position
    metadata on every stop move, so it survives a restart.
  - A peak-giveback exit stamps its floor and its peak as prices,
    `peak_giveback_floor_price` and `peak_giveback_peak_price`, beside the
    `peak_giveback_floor_r` / `_peak_r` it had (EXIT_CONTEXT carries the
    `peak_giveback_*` stamps). The reason strings are unchanged.
  - ENTRY_CONTEXT of a laddered entry: `ladder_rungs` (each rung with the
    R:R the builder measured from the signal close, `entry_price_model`),
    `ladder_active_index` (the rung the entry targets: rung 1, except on a
    key_levels strong setup, which targets rung 2) and
    `ladder_target_rung_rr_from_fill`, that rung's R:R from `fill_price`: its
    distance past the fill over the fill's distance to the stop, below 0
    when the fill is already past it, absent without a finite fill short of
    the stop.
  - Tests: `tests/test_exit_levels_logging.py` (new).

- **The `shared_entry` and `shared_exit` knobs are global: one entry stage
  and one exit policy for every strategy.** *2026-09-24* — every shared knob
  now acts on every strategy whose preset sets it, and no strategy has to
  call anything to get it.

  Until now each strategy called the knob helpers it chose to, so most knobs
  did nothing for most strategies. `peer_confirmed_key_levels` reached three
  of them. The reversal strategies never met the dual-divergence veto, and
  only `top_tier_adaptive` met the candle filter. `microcap_pm_breakout` and
  both 0DTE strategies never met a score term. Several strategies had
  re-implemented a knob under a param of their own, and a
  `strategy_logic_default` hook let any strategy rewrite any knob. On the
  exit side, the peer family's `position_exit_signal` override silently
  dropped the time stop and five exit families whatever the YAML said.

  **Entries: `_strategies/shared_entry.py`.** `SharedEntryPolicy`, built by
  `BaseStrategy.__init__` as `self.entry_policy`, is the only reader of
  `config.shared_entry`. A strategy keeps its setup logic, alternatives and
  selection. For each alternative it builds an `EntryProposal`: the market
  direction, style, style family, close, stop and target, the frame each
  read is pinned to, its own blockers as pending reasons, and an optional
  FVG / order-block `RetestTrigger`. It hands the proposal to `admit`, which
  runs, in order:
  - the raw R:R gate a builder asks for;
  - the contexts on the pinned frames;
  - the retest admission;
  - every switched-on veto in `VETO_GATES` order (structure, S/R, broken
    level, chart, dual divergence, candle), minus any the manifest exempts
    for the style. All of them are evaluated, so a refusal lists every
    blocker;
  - the S/R then the technical refinement, and the retest stop anchor;
  - the score terms and the optional `min_shared_context_score` floor.

  `emit` builds the `Signal` from the admitted levels. It is the only
  `Signal(...)` in the code base. It stamps `entry_style_family`, `regime`,
  `strategy_priority_score`, `shared_context_score`, `final_priority_score`
  (their sum), `entry_price` and the `shared_entry_*` fields. `rank_key`
  ranks the gatekeeper's signals, and `divergence_entries` builds the opt-in
  divergence-only entries.

  **Exits: `_strategies/shared_exit.py`.** `SharedExitPolicy`, owned by the
  position manager, is the only reader of `config.shared_exit`. Each cycle
  it runs, in order:
  - the shared families in `EXIT_FAMILY_GATES` order: time stop, chart
    pattern, candle pattern, CHoCH, bias structure, technical, S/R loss.
    Each family sits behind one table row of gates (R gate, hold grace, ORB
    grace, post-entry pivots, post-entry event, bar range, tape) instead of
    a 240-line method;
  - the strategy's own `strategy_exit_signal` hook (the peer ladder
    defence, the microcap blowoff guard);
  - the divergence scale-out, last so any full exit wins.

  Exits are `models.ExitDecision(reason, family, fraction, marker)`.

  **A strategy's only say is declarative**, in its manifest, validated at
  load:
  - `capabilities.shared_entry`: `exemptions: {style: [gates]}` and
    `divergence_entry: false`;
  - `capabilities.signal_priority`: `primary_field`, `shared_score_weight`,
    `rank_unit_field`, `metadata_fields`.

  Shipped exemptions: top_tier / small_cap_squeeze `{orb: [structure, sr]}`,
  htf_pivots `{pivot_rejection: [structure]}`, and zero_dte_etf_options
  `{midday_credit_spread: [structure]}`. 2026-09-25 added range / pullback /
  sr_scalp (structure) to both top_tier-engine manifests and vwap_reclaim
  (S/R) to top_tier's (see Changed). Divergence-only entries are off for
  pairs_residual and both 0DTE strategies.

  **Enforced by tests.** `tests/test_shared_knob_contract.py` (256) fails any
  module that:
  - reads either section outside the two policies;
  - constructs a `Signal` or `AdmittedEntry`;
  - uses a helper that moved into the policy;
  - rewrites what `emit` built (`dataclasses.replace`, assigning
    `.stop_price` / `.target_price`);
  - reaches into the policy's privates.

  `BaseStrategy.__init_subclass__` raises `TypeError` for
  `position_exit_signal`, `shared_exit_signal`, `strategy_logic_default` and
  `signal_priority_key`. `tests/test_knob_reach_matrix.py` (425) switches
  every veto, the score floor, the five score terms, both refinements and
  every exit family on and off for all 17 strategies, and runs a
  divergence-only entry end to end through every capable one. Per-area tests:
  - `test_shared_entry_policy.py` (144) and `test_shared_exit_policy.py` (89);
  - `test_partial_exit.py` (38) and `test_divergence_session_age.py` (46);
  - `test_peer_shared_entry.py` (65), `test_breakout_conversions.py` (133),
    `test_shared_entry_microcap_pairs.py` (67) and
    `test_zero_dte_shared_entry.py` (41);
  - `test_preset_parity.py` (68).

  The rank order is pinned against the real pre-change ranker
  (`tests/fixtures/rank_golden/`). Full suite: 4146 passed.

  **Replayed against the pre-change tree**, per strategy family. Every
  difference found is one of the intended changes below:
  - top_tier: 0 decision changes; 324 refusals record a different primary
    reason.
  - Breakout family: every decision difference over 3,800 runs is an
    intended one (the chart veto on retest-admitted entries, the anchor
    re-clamp, the vol_squeeze tier).
  - Peers: identical once the divergence clock is held fixed.
  - 0DTE: 0 unexplained differences over 10,080 decisions.

  Docs: the root README's `shared_entry` / `shared_exit` sections (every
  knob, the stage order, the ranking, a per-strategy wiring table),
  `_strategies/README.md` (the author contract) and
  `configs/README_PRESETS.md` (the preset matrix).

- **Partial closes.** *2026-09-24* — an exit decision can close part of a
  position. `ExitDecision.fraction` below 1 is a scale-out of the current
  quantity:
  - The position manager sizes the slice with a floor
    (`shared_exit.partial_exit_qty`). A 1-lot option or 1-share position
    cannot scale out, so the trigger is spent without an order. The product
    is rounded to 9 places before the floor, so 100 x 0.29 closes 29, not
    28.
  - It cancels a resting broker bracket first, then sends
    `execution.close_position(position, qty)`. `qty` is now required; the
    method always sent the whole quantity before.
  - It books the slice as a partial leg (`per_partial_exit_reason` in the
    session report).
  - It re-protects the remainder at the broker at the current engine
    levels: at once when the slice books, and in full when the slice never
    reached the broker (a rejected submit, a stale quote), since nothing of
    it can fill. One that reached the broker with no order id to track may
    still fill, so the engine stop keeps owning that position.
  - A slice left working at the broker (a halt, an unconfirmed cancel):
    the shares it does not cover are re-protected at once, and everything
    still held once it settles. The re-protect adopts and resizes a live
    bracket (the one placed beside the slice, or the one a restart put on
    the position; since 2026-09-25 a restart sizes that one to the shares
    outside the slice, see Fixed) instead of stacking a second OCO on it.
    While the slice works, the engine still runs its risk check on the
    shares outside it; a risk exit cancels the slice, and the cycle that
    settles it sends the full exit. Before, the position was skipped until
    the slice settled, and settled without re-protecting the remainder,
    which then had no broker bracket for the rest of the trade.
  - A full exit that part-fills with its cancel unconfirmed and then dies
    re-protects what it left the same way. Before, that remainder had no
    broker bracket unless the family that decided the exit fired again.
  - It records the decision's one-shot marker in
    `metadata['<family>_exits']`.

  Force flatten turns a pending scale-out into a full exit. The divergence
  scale-out is the only shipped user, and it is off in every preset.

- **Divergence entries and the divergence scale-out are wired centrally,
  and ship off.** *2026-09-24* — the opt-in divergence triggers were helpers
  no strategy called, so `use_divergence_entry_signal` and
  `use_divergence_exit_signal` did nothing. Both now run for every strategy
  when switched on; both are `false` in every preset.

  **Entry side.**
  - The stage reads one candidate per side, once per symbol per cycle. A
    candidate on a proposal's own side adds `divergence_entry_score_bump`;
    one only on the other side is stamped as a conflict. Before, only the
    better side was kept, so a SHORT the strategy could not take hid a
    valid LONG.
  - A symbol the strategy produced no signal for may take a divergence-only
    entry, which passes every veto, the refinement and the score floor and
    always ranks behind every strategy signal (`rank_key` tier 0).
  - It never opens a symbol the strategy skipped before evaluating a setup.
    `shared_entry.DIVERGENCE_INELIGIBLE_REASONS` lists those tokens: not its
    symbol, outside its window, too few bars (`insufficient_*`), top_tier's
    macro / earnings blackouts, `shorts_disabled`, and the relative-strength
    sector skips.
  - The candidate's latest pivot must be in the reader's current session.
    With the new session clock, yesterday's closing divergences are young at
    09:30, and a stop anchored across the gap is not a structural stop.
    Recency is 1 - age / max age instead of / 8, OBV divergences are read,
    and a hidden divergence needs the HTF EMAs aligned.
  - The stop buffer is `support_resistance.stop_buffer_atr_mult`; it was read
    from a `technical_levels` key that does not exist. A capped target must
    still clear `min_target_rr`.
  - pairs_residual and both 0DTE strategies opt out in their manifests.
    `microcap_pm_breakout` stays capable, which its README documents.

  **Exit side.** The latest pivot must have closed after the entry: a
  divergence already on the chart when the trade was taken used to scale it
  out on its first in-profit cycle. It fires once per divergence pivot,
  whichever indicator saw it. RSI and OBV share their price pivots, so the
  old per-indicator key closed 75% of the position at the default fraction.
  "Counter" is judged against the trade's market direction, so a bull put
  spread watches for a bearish divergence.

- **New config surface.** *2026-09-24*
  - `shared_entry.use_broken_level_guard` (default and every preset `false`),
    with `broken_level_min_clearance_pct` (0.0025) and
    `broken_level_min_clearance_atr` (0.72), moved from top_tier (see
    Changed).
  - `shared_entry.min_shared_context_score` (default `null`): an optional
    floor on an entry's shared score, refused as `shared_context_below_min`.
  - `shared_entry.min_target_rr` / `min_stop_atr_mult` accept `null`, and `0`
    now switches them off; a configured `0` used to read as the default.
  - `technical_levels.htf_divergence_max_age_bars` (default 6; see Changed).
  - `peer_confirmed_key_levels` / `_1m` param `require_peer_target_clearance`
    (default `true`).
  - Manifest `capabilities.shared_entry` and `capabilities.signal_priority`
    (`shared_score_weight`, `rank_unit_field`).

- **The audit logs keep the shared stamps.** *2026-09-24* —
  `EntryGatekeeper.structured_metadata_snapshot` now keeps
  `entry_style_family`, `strategy_priority_score`, `shared_context_score`,
  `entry_context_adjustment`, `technical_entry_adjustment` and
  `entry_source`, and the `shared_entry_*`, `divergence_entry_*` and
  `anti_chase_ob_retest_*` prefixes, so `events.jsonl` can answer which
  gates ran, which were exempt and what the shared score was.

### Changed

- **`MarketDataStore.fetch_quotes` writes every REST quote through one
  helper, `_store_rest_quote`, and reads the quote TTL from `_quote_ttl`.**
  *2026-10-06* — the batch, the single-quote fallback and the alias fetch
  each stamped `fetched_at` and cached the quote and the symbol's
  `last_quote_refresh` in a copy of the same block; they call the helper now,
  each with its own `fetched_at`. `_quote_ttl()` is
  `max(1, runtime.quote_cache_seconds)`, which `should_refresh_quote` computed
  inline. No behavior change: the stream quotes' publication (the L1 cut)
  builds on both.
  - Identical: the 10-01 09:40-10:40 stepped replay (top_tier, a page open)
    against 2d70ab2, in every category.
  - Tests: `tests/market_data/test_quote_store.py` (new: the TTL and its
    floor, a quote due at exactly the TTL, each REST path's stamp, the
    helper's, a failed fetch leaving the cache as it was). 9 mutants, all
    killed, each by its named test.

- **The equity curve samples by time, not by pass
  (`paper.equity_point_seconds`, new, default 15).** *2026-10-05* — the
  engine samples the paper account once a pass (the idle entry below: the
  dashboard build used to), and every sample added a curve point, so at
  this release's ~2.5 s RTH passes the 2,000 points of
  `paper.max_equity_points` held only the session's last 80-90 minutes
  (4-4.5 hours at 21032f8's 7.6-8.2 s passes): the dashboard's sparkline
  showed them, and the 20:00 archive's `account_snapshot.json` curve
  started about 14:40.
  - `PaperAccount.record_equity_point` moves the peak and the max drawdown
    on every call, as before, but adds a curve point only when the curve
    is empty, its last point is at least `equity_point_seconds` old, or
    that point is stamped after the call (a clock set back, as in the
    fall-back hour of a wall clock: the cadence restarts from the new time
    instead of leaving the curve without points until the clock passes the
    old one). A call at the last point's time still replaces it, and a
    call that adds no point builds none. Its new keyword `force_point`,
    required, adds the point whatever the interval: the engine's pass
    passes False and `capture_snapshot` (the session report's and the
    archive's read) True, so the archive's curve ends on the capture
    moment and the cadence runs on from it. `PaperAccount` takes
    `equity_point_seconds` (default 15, the config's); the engine passes
    the config's.
  - `paper.equity_point_seconds`: a number of at least 0, checked at load
    like the section's other numbers; 0 adds a point on every sample, the
    behaviour before. At 15 the session's ~2.5 s passes add a point every
    15-17.5 s, so the 2,000 points span at least 8.3 hours, and the idle
    passes add one a minute: both the dashboard's curve and the 20:00
    archive's hold the whole session. The sparkline's last point is up to
    that old; the equity figures beside it are read live, as before.
    `config.example.yaml` and README's `paper` table carry it; the other
    presets take the default.
  - Identical: the 10-01 09:40-10:40 stepped replay with a page open,
    against 21032f8 and against the previous commit, in every category,
    the dashboard's included: its passes are 20 s apart, so each still
    adds its point. Stepped at 5 s (10-01 09:40-09:50), only the
    dashboard's payloads differ from 21032f8's, by their curve (a point
    every third pass); with the knob at 0 that window is identical.
  - README: `max_equity_points`, `equity_point_seconds`, and the idle
    cadence's and the heartbeat's notes on the curve; the idle entry below
    says what the curve held as first built.
  - Tests: `tests/runtime/test_paper_account_sampling.py` (calls 5 s apart
    at 15 add points at 0, 15, 30 and 45 s, each its own call's equity; a
    call that adds no point still moves the peak and the max drawdown; 0
    adds one on every call, one stamped before the last included; a clock
    set back adds one at once; the capture always adds its point and the
    cadence runs on from it; `force_point` has no default; the account's
    default is the config's), `tests/composition/test_idle_gate.py` (2.5 s
    passes through the real engine add a point every 15 s, and a loss on a
    pass that adds none reaches the max drawdown),
    `tests/composition/test_dashboard_state.py` (the engine builds the
    account with the config's cadence) and
    `tests/domain/test_config_validation.py` (the knob's number check; a
    negative, a word and a null refused at load; 0, 2.5 and 60 load);
    `test_dashboard_update_failure.py` and `test_engine_shutdown.py`
    follow. 22 mutants, all killed, each by its test alone.

- **top_tier's entry pass reads each sector frame's posture and session
  open once a pass; the event calendars' path is resolved without four
  `resolve()` calls.** *2026-10-05* — the entry pass scanned each of the
  ~25 peer and ETF frames ~17 times (every candidate of its sector, for
  both sides: ~390 posture reads and the leg-anchor scan behind each) and
  read the session open of ~28 frames 86 times.
  - `ConfirmationMixin._pass_memo_read` (new) keeps a frame read for the
    rest of one `entry_signals` call, keyed on the frame's context-cache
    key (id, length, last bar) and every other input the read uses, and
    pinning the frame. `entry_signals` opens it and drops it in a
    `finally` (its body is now `_entry_pass`); outside a pass every read
    runs as before.
  - `_frame_agrees` compares the side with `_frame_posture` (new), the
    side-free `bar_posture` of the latest bar against the leg-anchor
    reference, memoized on the clock's date, the session start and the
    three `leg_*` params. `_day_strength_session_open` (now an instance
    method) is memoized on the date and the session start; it also serves
    the momentum regime's per-side lookups. `SmallCapSqueezeStrategy`
    inherits both.
  - `event_blackouts._resolve_path` returns the configured path as given
    when it exists, which the candidate search always chose then; the
    package- and project-root candidates are built only when it does
    not.
  - Measured (CACHE-PASS verifier, base 9c2a8d2, 9 interleaved pairs on
    09-30..10-02): entries 1.28 → 1.01 s, step CPU -0.23 s; on top of the
    array helpers below -0.13 s a pass (entries 0.85 → 0.73 s); the path
    resolve 181 → 10.5 µs a call.
  - Identical: the verifier's stepped replays (top_tier 10-01 09:30-11:30
    with 20 trades, 10-02 across the 15:00 entry close, small_cap 06-02)
    with every one of 103,539 memo hits recomputed and compared, 0
    mismatches; here, the 10-01 09:40-10:40 stepped replay against
    21032f8.
  - Tests: `tests/strategies/top_tier_adaptive/test_entry_pass_memo.py`
    (new): each peer and ETF frame read once a pass, both sides from one
    posture, a frame grown in place within a pass, a new date and a
    flipped `leg_anchored_confirmation` read fresh, a second pass and a
    frame changed after a pass read again, the memo open only during the
    pass and dropped when it raises, one memo per strategy, and the path
    resolve's three cases. 9 mutants killed; the two the verifier found
    equivalent survive as expected.

- **The dashboard snapshot's bars and the HTF trend row are kept across
  passes and rebuilt only on a new bar or HTF refresh.** *2026-10-05* —
  with the step frames and contexts kept (below), a built publish with no
  new bar still rebuilt every shown symbol's snapshot bars (the bars, the
  per-bar candle map, the strategy's LTF EMAs from a second frame read)
  and its HTF trend row from frames that had not changed.
  - `DashboardCache._snapshot_bars` keeps one build per symbol, keyed on
    the 1m frame's version (`bars.frame_version`: the store frames it was
    built from and its variant), its length, last bar and columns, the bar
    count, the strategy's LTF and LTF EMA request, the indicator settings
    and the candle lists. A build is kept only when the strategy's LTF
    EMAs came from the same store frames as the 1m frame (a stream bar can
    land between the two reads) and its candle map did not fail. Hits and
    keeps copy each bar and its candle lists. A frame with no version is
    built every time, as before. The publish's and the engine's prunes
    drop a symbol's entry. `_apply_strategy_ltf_emas` returns the source
    token of the frame its EMAs came from.
  - `MarketDataStore.derive_from_htf_frame` (new) keeps a build of the
    stored HTF frame per symbol, timeframe and slot until the stored
    object (every refresh stores a new one) or the indicator settings
    change; the build gets a shallow copy. `sr_snapshot._htf_trend` goes
    through it, so the dashboard's S/R row and the exit record's HTF trend
    read it, and the prune drops it.
  - The LTF fair-value-gap overlay is served by the FVG memo below; the
    technical overlay is still built at the live quote every publish.
  - Measured (CACHE-DASH verifier, on 9c2a8d2 with the kept step frames,
    interleaved): a built publish with no new bar 1.05 → 0.72 s, a pass
    -0.32 s weighted; real-time manage_gap 5.29 → 5.01 s. With the context
    memos too, the no-bar publish took 0.54 s (probe).
  - Identical: the verifier's three stepped replays (the 09-30 open with
    16 trades, 10-01 across the entry cutoff and the close, a peer preset)
    with every bars-memo hit and every HTF-memo call compared with a fresh
    build (0 mismatches in 10,724 and 16,585), and 672 `/api/chart`
    payloads equal; here, the 10-01 09:40-10:40 stepped replay against
    21032f8 with a page open.
  - Tests: `tests/reporting/test_snapshot_frame_memo.py` (new): a hit
    equals a rebuild without building; a new bar, a revised old bar and
    every other key input rebuild; a span-5 hand-out of the same store
    frames is not served the canonical bars; EMAs read from other store
    frames and a failed candle map are not kept; a frame without a version
    builds every time; hand-outs never share the memo's bars; the prunes;
    the whole snapshot equals a fresh cache's; the HTF trend memo's object,
    settings, slot, copy and prune. `tests/support/brokers.py`'s fake feed
    answers `derive_from_htf_frame`. 22 mutants, all killed.

- **The S/R, fair-value-gap, order-block and strategy contexts are kept
  across passes and rebuilt only when what they read changes; a shadow
  check (`runtime.context_memo_shadow_every`, on at 20) re-proves them.**
  *2026-10-05* — with the step frames kept (below), a pass with no new bar
  still rebuilt every symbol's S/R, its LTF fair value gaps and the
  strategy's chart, structure and technical contexts from frames that had
  not changed: the S/R and contexts phases, 0.67 + 1.13 s of a production
  pass.
  - `context_memo.ContextMemo` (new) keeps one build per slot (a builder
    and the request it answered: symbol, timeframe, parameters, the
    frame's variant) under a key of every input the build read: the frame
    it read (its `bars.frame_version`, or for the S/R the stored HTF frame
    object, read once and pinned), the request, and the clock only as the
    build reads it: the completed bars (`bars.forming_positions` for the
    fair value gaps, `support_resistance.flip_frame_clock_key` for the S/R
    flip frame: its completed 1m bars and the 5-minute slot, none for an
    empty one), the session date (S/R), the structure frame's forming last
    bucket, and `indicator_clock_key` (the session indicator mode and
    window and whether the clock is inside that session, which the ATR and
    the divergence clocks switch on). A build is kept only when the clock
    part reads the same after it as before, so a minute or session
    boundary crossed mid-build files nothing.
  - An empty flip frame has no clock key: the build reads no clock through
    it. As first built (fixed in the cut's final review), the S/R key
    compared its index with the clock anyway, and `get_merged` hands a
    symbol whose 1m bars have not arrived an empty frame on a RangeIndex,
    so the read raised TypeError where 21032f8 built the context: on
    small_cap_squeeze 2026-06-02, with two candidates new at 09:30:40, two
    dashboard updates failed and their decision records lost 19 `mshtf_*`
    fields (no order, signal or trade changed). Every other key the cut
    added reads an empty frame as its build does.
  - The store (`MarketDataStore._level_memo`: S/R, FVG, order blocks) and
    each strategy instance (`_context_memo`: chart, structure, technical)
    have one. A slot not read for a whole cycle is dropped at the next
    (`begin_cycle`, `reset_context_caches`), the prune drops a pruned
    symbol's slots, and the per-cycle caches stay the first level. A frame
    without a version (a copy, a strategy-built frame) is built every pass,
    as before. The structure context keys on its frame after
    `bars.verified_frame`, so a step frame written after its hand-out is
    neither served nor kept.
  - A chart hit replays the build's one effect on its input (the
    clean-frame mark, `chart_patterns.mark_clean_input`), which is in the
    chart's key (`clean_input_marked`). The fair-value-gap and order-block
    contexts are shared instead of deep-copied, like every other context
    (no reader writes into one). `get_support_resistance` reads the stored
    HTF frame itself, once, instead of a copy through `get_htf_frame`.
  - `runtime.context_memo_shadow_every` (new; default `20`, and `20` in
    every preset): each memo hit is rebuilt with probability 1/N, drawn
    from a generator seeded on the memo's name, and compared with what the
    memo would serve, field by field and floats by bits; a difference logs
    CRITICAL `Context memo <name> served a context a rebuild does not
    give: slot=... key=...` and the rebuilt context is served and kept. A
    rebuild the clock key moved under is served, not compared or kept.
    Every 30 minutes with hits, each memo logs `Memo shadow <name>: R of H
    hits re-checked ... on S of the T slots hit, D differed` at DEBUG. At
    20 it rebuilds about 5% of the hits (5-6 of top_tier's 112 on a pass
    with no new bar, under 0.01 s of CPU there), and only detects: a hit
    it does not draw is served from the memo, so `1` (every hit rebuilt)
    is the setting after a CRITICAL line, until the key is fixed. It is on
    for the memos' first dry-run day; a day whose log holds no such line
    is the evidence, and then `0` turns it off (README). An integer of at
    least 0, checked at load. As first built (the stage's review fixed
    it), every N-th hit of a memo was re-checked: a pass reads its slots
    in a fixed order, so a fixed subset of them was re-checked pass after
    pass (on 10-01, 21 of top_tier's 28 S/R and chart slots never were);
    a per-slot count reset at each new key would have re-checked only the
    20th hit of a key, never the end of a 1m slot's minute.
  - Measured (CACHE-CTX verifier, on 9c2a8d2 with the kept step frames,
    interleaved): a pass with no new bar 3.07 → 1.83 s of CPU (sr 0.61 →
    0.008, contexts 0.44 → 0.02, publish 1.06 → 0.86 s), the new-bar pass
    unchanged; at real-time pace manage_gap 5.32 → 4.17 s; memory flat.
  - Identical: the verifier's four stepped replays (10-02 10:15-12:15 with
    16 trades, the 09-30 open, the 10-01 close across 16:00, a preset with
    order blocks on) with every one of 78,333 memo hits rebuilt and
    compared, 0 mismatches, and 7,472 more at real-time pace with bars
    landing mid-pass; here, the 10-01 09:40-10:40 stepped replay against
    21032f8 with the shadow on at 20, and no `Context memo` line in its
    log.
  - README: `context_memo_shadow_every`; `config.example.yaml` and every
    preset ship it.
  - Tests: `tests/market_data/test_context_memo.py` (new): each memo
    rebuilds when an input its key holds moves (the flip frame's bars and
    5m slot, the stored HTF frame, a rewritten older bar, the session
    date, the session switch, a forming bar completing, the 5m bucket, a
    new bar; and, added in the stage's review, the structure memo's frame
    token on a bar inside a forming 5m bucket and its session switch
    alone, the order blocks on a new bar, a 5m FVG's completion) and
    serves a fresh build's equal otherwise; an empty flip frame gets a
    build's context, kept across a 5m slot until the bars arrive (added in
    the final review); the fair value gaps key on the frame they read, not
    the store after it; the prune and the generation turn, and a slot read
    once a cycle built once; one slot per variant; the chart mark replay; a
    written step frame neither served nor kept by the structure memo; the
    shadow's CRITICAL line, its draws (about one hit in N, every slot
    whatever the pass order, every moment of a key's life), a rebuild the
    clock moved under served without an alarm, its half-hourly counts and
    its knob in both memos; a build the clock moved under is not kept;
    floats by bits; the default and every preset at 20.
    `test_config_validation.py` (the knob), `test_module_layering.py`
    (`context_memo` in layer 1), `test_sr_tolerance_reads.py` (the S/R
    build reads the stored frame). 30 mutants, all killed; the review's 17
    (the sampling, the clock guard, the summary, the four key parts and
    the carry-over), all killed, the three sampling counts also by the
    sampling tests alone; and the final review's 2 (the empty flip frame's
    key), killed.

- **The step frames are kept across passes: `get_merged` rebuilds a frame
  only when the store's bars for it change.** *2026-10-05* — every pass
  merged and rebuilt the indicators of 84 frames (28 symbols x the 1m step
  frame, the strategy's span-5 LTF and the 5m structure frame) whether or
  not a bar had closed; about six of seven passes have no new bar.
  - `MarketDataStore.get_merged` keeps each frame it builds
    (`_merged_memo`, under the per-cycle cache's keys) with the stored
    history and live objects it was built from and the two process-wide
    indicator settings, and serves it while the store holds those very
    objects. Every writer of the 1m store (`fetch_history`, the stream's
    `on_stream_message`, the prune) stores a new object and never writes
    into one, so the same two objects hold the same bars. The enriched
    build reuses the base build of the same objects. The per-cycle cache
    stays the first level.
  - Every frame handed out is a shallow copy (pandas 3 copy-on-write keeps
    a caller's writes out of the kept frame) registered with its version:
    a token naming the (history, live) pair it was built from (a
    generation never reused in the process) and the timeframe, and the
    variant (indicators or not, span scale, EMA spans, the indicator
    settings). Each per-cycle cache entry is a `(frame, version)` pair, so
    a frame built before a stream bar landed keeps the pre-bar token for
    the rest of that cycle, never the store's newer one.
    `bars.frame_source_token` and `bars.frame_version` read them; the
    registry also holds the frame each hand-out was copied from (shared
    arrays, no bar memory), and an entry goes with its frame, at shutdown
    too. `get_htf_frame` hands out a shallow copy too.
  - The strategy's 5m structure frame (`_resampled_frame` without `data`)
    is kept on the step frame's token and its own inputs (`tf`, span
    scale, EMA spans, the indicator settings; `bars.derived_frame`). The
    structure context reads its frame through `bars.verified_frame`: a
    step frame whose index or OHLCV columns no longer share memory with
    the frame it was handed out from (a `.loc` / `.iloc` / `.at` write, a
    replaced column or index, an appended row) is analysed as an
    unregistered copy, so its structure frame is built from the bars it
    holds, never served a kept one (28 checks a pass, about 5 ms; no code
    writes into a step frame today).
  - `prune_inactive_symbols` drops a pruned symbol's kept frames,
    generation and structure frames.
  - Measured (CACHE-FRAME verifier, base 9c2a8d2, interleaved): a pass
    with no new bar 4.87 → 3.03 s of CPU, the new-bar pass +0.16 s (it
    builds what it built before while the old frames are still held);
    real-time pace, manage_gap 7.40 → 5.31 s; no-bar `add_indicators`
    calls 84 → 0; max RSS +23-30 MB (the kept frames, 17 MB at the open
    to 21 MB at the close).
  - Identical: the verifier's stepped replays (10-02 10:15-12:15 with 16
    trades, the whole 10-01 session, a peer preset's day) with every
    audited hand-out, kept frame and structure frame compared with a fresh
    build (0 mismatches) and every stored frame re-digested on every
    sighting (0 in-place writes); here, the 10-01 09:40-10:40 stepped
    replay against 21032f8.
  - Tests: `tests/market_data/test_merged_frame_memo.py` (new): every
    writer of the store replaces its object; the kept frames, their
    variants, the indicator settings, a caller's writes, the prune, the
    tokens and a bar landing mid-build; every per-cycle cache write on
    every read path is a `(frame, version)` pair and `get_merged` is its
    only writer (a static check of the package); the versions tell a
    pair's variants apart; and a step frame written after its hand-out
    (six kinds of write) gets the structure context of the bars it holds;
    a new bar replaces the symbol's structure frame and frees the old one
    (added in the stage's review, its mutant killed); a frame freed at
    shutdown drops its entry without an error.
    `test_hot_helpers_pinned.py` compares the cycle cache's pairs. 29
    mutants, all killed (the verifier's 14 and 15 for the versions, the
    cycle cache's pairs, the bars check and the registry's shutdown).

- **The engine idles whenever nothing needs its watchlist, inside the stream
  window too, and builds the dashboard state only for a reader
  (`dashboard.client_idle_seconds`, `dashboard.idle_publish_seconds`).**
  *2026-10-05* — from 15:55 to 20:00, and from 07:00 to the prewarm after an
  overnight run, every 2 s pass built every symbol's frame, S/R and contexts
  on frames nothing changed or read: 4.9-5.1 s of work every 7 s, about 70%
  of a core for 4-6 hours a day (the core seen at 100% while the bot sat
  idle). And every RTH pass built the dashboard state, 1.54 s of a 7.9 s
  production pass, whether or not a page was open.
  - The cycle gate (`CycleGate._should_idle_closed_market_watchlists`)
    idles the watchlist whenever nothing consumes it: no position, and no
    screener, management or entry window, stream or prewarm. Until now the
    broker's 07:00-20:00 stream window kept it. It never idles with a
    position, a window, the stream or the prewarm. An idle pass has an
    empty watchlist and quote watchlist, as after 20:00: no frame, S/R or
    context is built, and the dashboard keeps the last screener
    candidates' cards and drops the rest (25 of top_tier's 28 symbols on
    10-01). The stored bars stay, as they did overnight.
  - An idle pass sleeps `runtime.idle_sleep_seconds` (60) inside the stream
    window too, but no later than the gate's next wake
    (`CycleGate.idle_wake_at`: the next window's start less
    `prewarm_before_windows_minutes`), so the prewarm, or a window with no
    prewarm, starts on time; top_tier wakes at 09:15:00. A pass that fails
    before its gate keeps `loop_sleep_seconds` and its backoff.
    Housekeeping that falls due while idle (the 20:00 archive, a reconcile
    retry, the auto-exit check) comes up to a minute later.
  - The idle status reads `Idle until next session window`, day and night
    (the night read `Market closed`, which from 15:55 would be said of an
    open market); `CycleGate.runtime_status_message` takes no idle flag.
    The log line is `Watchlist idle strategy=<name>
    reason=idle_no_window_or_position`, every 5 minutes while idle (it was
    `reason=market_closed_outside_broker_session`, at night).
  - The dashboard state (the page, `/api/state` and the state file) is
    built while a client polls: a request for the page, `/mobile`,
    `/api/state` or `/api/chart` in the last `dashboard.client_idle_seconds`
    (new, default 30; above 0 and at least the slowest page's poll,
    `max(refresh_ms, 4000) / 1000`, since `/mobile` polls no faster than
    every 4 s; checked at load). It is also built on a new status or
    message, while a build is failing, on a failed cycle's error path, and
    otherwise once every `dashboard.idle_publish_seconds` (new, default
    60; above 0, checked at load). Unwatched, `/api/state`, the state file
    and a page that opens first are up to that old; the pass after the
    page's first poll builds a fresh one. With the dashboard off nothing is
    built, so a broken build is no longer logged there.
  - `PaperAccount.record_equity_point` (new) samples the peak, the max
    drawdown and the equity curve, and the engine calls it on every pass,
    before it decides on the build. `PaperAccount.snapshot` (new) reads the
    account without sampling it; `snapshot_copy`, the dashboard's read,
    uses it, and `capture_snapshot`, the session report's and the
    archive's, does both. The build used to sample the account, so built on
    demand it would have left the report's max drawdown and the archive's
    curve to whether a page was open. Off-window the account is now
    sampled once a minute instead of every 7-8 s, so the after-hours
    passes no longer push the session out of the 2,000 points the 20:00
    archive writes. The peak and the max drawdown follow every sample and
    cover the whole day; the curve gets a point every
    `paper.equity_point_seconds` (the curve entry above). As first built
    every sample added a point, which at this release's ~2.5 s passes held
    only the session's last 80-90 minutes: the 20:00 archive's curve
    started about 14:40 on top_tier.
  - The cycle builds every HTF context the strategy and its dashboard rows
    read (`htf_context_requests`, new: the score context's
    `_default_htf_request`; the peer family's own `_symbol_htf_request`;
    and `generic_htf_trend_request`, the trend the S/R row shows for a
    strategy with none of its own, which `sr_snapshot` now reads from the
    strategy) for every step-frame symbol in its contexts phase
    (`IntradayBot._prime_strategy_htf_contexts`), and again, for the
    symbols it refreshed, after an HTF refresh before management, the
    entries or the publish. A context carries the price of its first build
    until the next HTF refresh (the price is not part of the
    `MarketDataStore.htf_cache` key), and the dashboard build was that
    first build on 52 of top_tier's 56 contexts. Skipped, the first read
    would have moved into a later entry or management pass, at a later
    price: every logged `htf_ema_votes` changed, and with
    `require_htf_ema_alignment` or `htf_ema_alignment_score` on (off in
    every preset) entries would have depended on whether a page was open.
    Built at fixed points of the cycle, no HTF context's price depends on
    whether a page is open: in every shipped preset the dashboard reads no
    HTF context the strategy does not list. As first built, the step
    primed the score context only, in the contexts phase only (fixed after
    the stage's review): the peer family's gates, votes and scores read its
    own context, which then still went to its first reader (a GOOG long on
    2026-05-05 scored 1.25 higher with the dashboard off), a refresh before
    the publish left the refreshed contexts to the publish with a page open
    and to the next pass without one, and htf_pivots' S/R-row trend went to
    its first reader. The staleness itself predates this and is logged
    separately.
  - Measured (IDLE-1 verifier on 9c2a8d2, interleaved A/Bs): an off-window
    pass 3.25 → 1.46 s of CPU, sleeping 60 s instead of 2 s, 62% → 2.4% of
    a core; on the stage-1 tree above, 0.40 s a minute (0.7%). The idle
    heartbeat equals the idle cadence, so every idle pass still builds the
    state, at night too. RTH: step CPU 4.95 → 3.79 s unwatched, 4.97 s
    watched (unchanged); H:'s logged polls replayed on its passes save
    1.43-1.65 s a pass on an unwatched day (0.89 s on watched 09-29); on the
    stage-1 tree, manage_gap 3.88 → 3.50 s on the harness.
  - Identical: stepped replays of 2026-10-01 against 9c2a8d2, the page
    open (09:40-10:40, 145 steps, the payload hash equal on every step) and
    unwatched with the HTF prime (09:40-11:40, 289 steps, every category,
    the account and the dashboard on all 97 shared builds); a loop replay
    of the gate alone (without the demand-gated build and the HTF prime)
    from 10-01 15:45 through the night to 10-02 09:40, identical on every
    pass from the 09:15:00 prewarm through the 09:35 entries; a gate sweep
    of 19 presets x 5 days x every minute, flat and holding: 0 idle passes
    with a consumer. And here, stepped replays against 21032f8: 10-01
    09:40-10:40 with a page open, identical; 10-01 15:30-16:30, identical
    through 15:55:00 and then different only by the idle passes (the page's
    empty watchlist, the idle line every 5 minutes; the status message is
    the one the base showed), and unwatched also by the builds the demand
    gate skips (each heartbeat build equal to the base's at that step);
    10-02 08:50-09:40, identical from the 09:15:00 prewarm on, its trade
    included, but for the two `Watchlist trace` lines, logged at 09:15:00
    (the first non-empty watchlist) instead of 08:50:00. After the review's
    fixes, peer_confirmed_key_levels 2026-05-04 09:40-10:40 across its
    10:30 refresh: decisions, signals and trades identical, watched and
    unwatched.
  - README: `idle_sleep_seconds`, `prewarm_before_windows_minutes`,
    `max_equity_points`, the `dashboard` table, `client_idle_seconds`,
    `idle_publish_seconds` and the failed-update note;
    `dashboard_assets/README.md`: which requests count as a client;
    `config.example.yaml` ships both knobs.
  - Tests: `tests/runtime/test_cycle_gate.py` (the gate idles inside the
    stream window; each window, the prewarm, a position and each consumer
    alone keep the watchlist; the wake; the idle message);
    `tests/composition/test_idle_gate.py` (new: the cadence through the real
    loop, each consumer's fast cadence, the wake cap and its floor, a wake
    passed while the pass ran, a failed pass, the idle wait that blocks the
    loop thread instead of spinning it, the candidates' cards; the build's
    heartbeat, client window, status, retry and error path, and the
    account, its open positions at their marks, sampled on every pass);
    `tests/runtime/test_paper_account_sampling.py` (new);
    `tests/composition/test_htf_refresh_points.py` (after a refresh every
    HTF context build is the contexts phase's, watched or not, on top_tier
    and on a peer preset, whose own context is among them; a refresh before
    the entries or the publish builds the refreshed contexts at once);
    `tests/reporting/test_sr_snapshot.py` (the generic trend's request: EMA
    50/200 on the support_resistance levels and the strategy's HTF minutes,
    the arguments 21032f8's S/R row passed; added in the final review);
    `tests/reporting/test_dashboard.py` (the requests that stamp, through
    the real server); `tests/domain/test_config_validation.py` (both knobs
    and the slowest-poll cross-check, `/mobile`'s floor pinned to
    `mobile.js`); `test_dashboard_update_failure.py`, `test_cycle_timing.py`
    and `test_engine_shutdown.py` keep a page open or give their shell a
    dashboard and an account, and the dashboard-off test now pins that
    nothing is built and the account is still sampled. 51 mutants, all
    killed, and the final review's 12 on the generic trend's request.

- **The dashboard publish keeps the state it serves without copying,
  signing or indenting it; the state file is compact and throttled
  (`dashboard.state_write_seconds`); and `/api/state` answers an unchanged
  state with a 304.** *2026-10-05* — the publish took 1.54 s of a 7.9 s
  production pass; on the harness `DashboardServer.publish` took 0.22 s of
  a 1.18 s publish, about two thirds of that for the state file (its
  signature, the indented dump and the write).
  - `DashboardServer.publish` serialized the state compactly for
    `/api/state`, then deep-copied the tree into `DashboardState`, built a
    sorted signature of the whole state (to skip a file rewrite when only
    `last_update` and the API rates had moved) and serialized it once more,
    indented, for the file. Now `DashboardState` keeps the tree `json_safe`
    built (`get` still copies, for `publish_stale`), and the file gets the
    compact bytes `/api/state` serves. `_disk_state_signature` and
    `_API_USAGE_RATE_FIELDS` are gone.
  - The file is written at once when the status or the message differs
    from the last one written, so every `stale` and `error` state is
    written as before (its message carries the failure count and time),
    and otherwise at most every `dashboard.state_write_seconds` (new,
    default 30; `0` writes on every publish; a number of at least 0,
    checked at load). A state stays pending until a write of it succeeds:
    `stop()` writes the last published state the file does not hold (one
    the throttle held back, or one whose write failed); a failed write
    there is logged with its type and the server still stops. Nothing in
    the bot reads the file.
  - `DashboardCache.symbol_snapshot` stores the payload it hands out, not a
    deep copy: nothing changes a snapshot it is handed. The chart cache
    keeps its copy (the HTTP threads serve it).
  - `/api/state` sends an `ETag`: a random nonce for the process and the
    publish's number, read with the bytes under one lock. A request whose
    `If-None-Match` is that ETag gets `304 Not Modified`, no body,
    `Cache-Control: no-store`. Both pages send it back. On a 304 the
    desktop page skips the parse and the redraw and runs only what moves
    with the clock (the uptime, a chart whose forming bar has ended, the
    chart cache's expiry); the phone page has nothing to redraw. A page
    keeps the ETag only once the state is drawn and drops it on any error
    or disconnect, so the next poll redraws in full.
  - Measured: publish CPU 422 → 99 ms a pass (IO-2 verifier, an in-process
    A/B on 2026-09-24 and 09-22 on a loaded host; about 0.22 s a pass on a
    quiet one); the snapshot store 28-35 ms a pass (IO-4); state-file
    writes about 1.6 → 0.17 GB an hour. The GIL time per poll does not
    move (about 0.1 ms of user time for a 200 and a 304 alike; over
    keep-alive a 304 takes 0.14 ms against 0.59 ms, the difference kernel
    send time). With a page open every pass publishes a new state, so a
    desktop page polling every 1.5 s gets a 304 instead of the 1.4 MB
    state on about 1 - 1.5/P of its polls for a P-second pass: about 40%
    at the ~2.5 s pass of this release, and nearly all while the loop
    idles; `/mobile` polls every 4 s and gains only from passes longer
    than that.
  - Identical: `/api/state`'s bytes on every step of the verifiers'
    replays (IO-2 125 steps, IO-4 106 steps and 9 idle steps with 175
    cache hits, IO-8 2 x 37 steps fetched over HTTP), and a stepped replay
    of 2026-10-01 09:40-10:40 against 21032f8 in everything but the state
    file. The pages were checked by their source only (the build host has
    no browser or JS engine): load both in a browser before relying on
    them.
  - README: the `dashboard` table, `refresh_ms`, `state_path` and
    `state_write_seconds`; `dashboard_assets/README.md`: the endpoint;
    `config.example.yaml` ships `state_write_seconds: 30`.
  - Tests: `tests/reporting/test_dashboard.py` drops the signature's tests
    and pins the throttle (the cadence, a new status or message at once,
    `0`, the write at stop and its failure logged with its type, a state
    whose write failed left pending for the next write and the stop, the
    knob at load), the stored payload, the ETag (a new one each publish,
    never repeated by another process, read with its bytes), the 304
    through the real server over `http.client` (keep-alive included), and
    both pages' conditional poll by their source;
    `tests/reporting/test_dashboard_cache.py` replaces the deep-copy test;
    `tests/composition/test_dashboard_state.py` pins that no publish
    changes a cached snapshot and that the engine passes the knob;
    `tests/domain/test_config_validation.py` lists it. 32 mutants, all
    killed.

- **The dashboard's bars are read a column at a time, and the candle caches
  are sized to a pass.** *2026-10-05* — every publish builds 48 bars for
  each of the 28 dashboard symbols, and every `/api/chart` up to 480.
  - `dashboard_payloads.bars_from_frame` reads each of its 23 columns once
    (`_BAR_COLUMNS`) instead of a row Series per bar (`iterrows`), and
    builds the same dicts, keys in the same order; a column the frame lacks
    reads None on every bar, as before. The tail is no longer copied
    (nothing writes it).
  - `candles._ohlc_subset`, behind every candle detector (the dashboard's
    per-bar tags and the strategies' candle context): when open, high, low
    and close are float64 it drops the rows with a missing field on the
    array; any other dtype takes the pandas path, unchanged.
  - `_talib_pattern_array_from_key` keeps 4,096 entries (was 1,024): a pass
    asks for 1,344 (28 symbols x 48 TA-Lib patterns), so the old size
    evicted every one before the next pass and a pass without a new bar
    recomputed them all; now it hits. `_ohlc_arrays_from_key` keeps 256
    (was 4,096): a key only hits inside its minute, and the old size
    filled with dead keys (29 a minute) until about 11:50.
  - Measured (PURE-DASH verifier, base 9c2a8d2, 9 interleaved pairs): the
    publish 1.155 → 1.025 s a pass (saved 0.140 s, 0.112-0.154), about
    0.19 s at production's pace; step CPU -0.165 s; `/api/chart`'s bars
    5.25x faster on the HTTP thread (18.2 → 3.5 ms a chart); process RSS at
    11:30 239 MB against 290 MB.
  - Identical: every publish's payload digest on a stepped replay of
    2026-10-01 09:30-11:30 (361 steps, 20 trades); 7,252 in-situ
    `bars_from_frame` calls, 15,379 subsets and 7,252 per-bar maps compared
    with the old code; every 1m and 15m frame of 37 archived days. Only
    frames the bot never builds differ: a duplicate column name or
    MultiIndex columns now raise (they published blank fields).
  - Tests: `tests/reporting/test_dashboard_bars_columns.py` and
    `tests/analysis/test_candle_subset_arrays.py` (new, 60 tests) pin the
    bars and the subset to frozen copies of the old code on recorded tapes
    and odd frames, `_BAR_COLUMNS` to the fields the bars read, the per-bar
    map to the per-value side rule, a second pass that must not miss, and
    the arrays cache's cap; 12 mutants, all killed.

- **The hot bar helpers read arrays instead of building pandas objects per
  call.** *2026-10-05* — the S/R build, the strategy contexts, the entry
  pass and the dashboard call the same small helpers thousands of times a
  pass (a cProfile census of one top_tier pass: `ensure_ohlcv_frame` 361
  calls, `session_mask` 560, the flip confirmation 1,106,
  `indicator_session_open` 252, `latest_atr14` 168). Each now does its work
  on numpy arrays, with the same result:
  - `bars.ensure_ohlcv_frame`: a frame already clean (OHLCV leading as
    float64, unique columns, a strictly increasing unique index, no missing
    OHLC or volume) is returned as a deep copy; every other frame takes the
    rebuild, unchanged.
  - `sessions.session_mask` and `rth_close_minute`: one `np.unique` of the
    wall-clock dates and one calendar lookup per date; the minute of day by
    integer division. New `sessions.session_open_at(ts, window)`, the mask
    for one instant, which `indicators.indicator_session_open` reads instead
    of building a one-element index.
  - `indicators.latest_atr14`: the last non-NaN value on the float64 array
    (among the session bars when they apply).
  - `bars.same_day_mask` / `time_gte_mask` on the wall-clock array;
    `session_open_price` finds the first bar by position;
    `session_bucket_floor` on scalars.
  - The S/R flip check takes the four bar tails it compares once per build
    (`levels_shared.confirm_tail` / `confirm_by_values`; `confirm_by_bars`
    keeps its signature for the HTF levels).
  - `fair_value_gaps.detect_fair_value_gaps` tests every triplet's formation
    and session at once and runs its loop on the candidates only;
    `technical_levels._populate_atr_context` reads the true range on arrays
    (a NaN-skipping max and mean, as the frame's `max(axis=1)` / `mean()`
    did); `EntryGatekeeper._safe_series_last` reads the column, then the
    last position.
  - `MarketDataStore.history_warmup_counts` gives the warm-up decision
    (`WarmupTracker.should_fetch_symbol_history`) what `get_history` and
    `get_merged(with_indicators=False)` reported, reading the history in
    place instead of copying it; the merged count still comes from
    `get_merged`, so the cycle cache holds what it did.
  - `same_day_mask` takes a date: a `datetime` or `Timestamp` raises
    `TypeError` (it matched no bar before, silently). Every caller passes a
    date.
  - Measured (PURE-HELPERS verifier, base 9c2a8d2, 12 interleaved pairs on
    09-30..10-02): step CPU 4.81 → 3.40 s a pass (saved 1.36 s,
    0.82-1.56); on top of the array `add_indicators` above 3.24 → 1.90 s
    (saved 1.33 s): sr 0.55 → 0.21, entries 0.86 → 0.39, publish
    1.07 → 0.74, contexts 0.55 → 0.34 s.
  - Identical: the 15 helpers were called 2,026,980 times inside the real
    `step()` (four windows, three presets) and each call compared with the
    old code on the same input, 0 mismatches; four stepped replays
    (top_tier 10-01 morning and 14:50-16:05, small_cap 06-02, zero_dte
    05-20) identical. On inputs no path builds: a NaT stamp in an index now
    raises in the RTH mask (it was masked off), and `time_gte_mask` reads
    one as False (it raised).
  - Tests: `tests/market_data/test_hot_helpers_pinned.py` (new) pins the
    session masks, `rth_close_minute`, `session_open_at` and
    `indicator_session_open` to frozen copies on every minute of the DST,
    holiday, early-close and weekend days; `ensure_ohlcv_frame` to its
    rebuild on frames with NaN prices or volume, unsorted or duplicate
    stamps and other dtypes; the ATR context to its frame-based body with
    NaN bars; and the warm-up counts to the two reads, with live bars, in
    and out of a cycle. 15 mutants, all killed; 5 of the verifier's 16 had
    passed the suite. `tests/runtime/test_warmup_tracker.py`'s fake store
    answers `history_warmup_counts`.

- **`add_indicators` builds its columns on float64 arrays and assembles the
  frame once.** *2026-10-05* — every step builds 84 indicator frames (28
  symbols x the 1m step frame, the span-5 LTF and the 5m structure frame),
  about 21 ms each, on every pass whether or not a bar closed, plus the HTF
  frames at a boundary. The build inserted ~30 columns one at a time, each a
  pandas Series operation, and grouped the session sums on a `ts.date()`
  per bar.
  - Now the same TA-Lib, rolling, `ewm` and grouped-cumsum kernels run on
    the same inputs in the same order, on arrays read once: the session key
    is each bar's wall-clock date as an int64, the session-reset EMAs run
    per day's run of session bars, `np.where` stands in for `combine_first`
    / `where` / `fillna` / `replace(0, nan)` with the same NaN rules, and
    the returns and differences are array shifts. The frame is assembled
    once (6-8 blocks instead of 31-32); a column the input already carries
    keeps its place. `vwap_signal` / `vwap` and the other derived pairs are
    no longer one shared array.
  - The span-scale stamp (`attrs["indicator_span_scale"]`) is written only
    for a stretched frame and dropped on a native build.
    `indicator_span_scale`, its only reader, reads an absent stamp as 1.0.
    pandas deep-copies a non-empty `attrs` into every frame and Series
    derived from a frame: about 37,600 copies a step, now about 700.
  - Measured (PURE-IND verifier, base 9c2a8d2): 21.05 → 4.65 ms a call over
    1,130 recorded inputs; step CPU 4.76 → 3.22 s a pass on the harness (12
    interleaved pairs on 09-30..10-02, saved 1.51 s, 1.11-1.70), new-bar and
    no-bar passes alike (frame 0.69 → 0.21 s, contexts 1.05 → 0.56, entries
    1.26 → 0.86, publish 1.20 → 1.08); an HTF boundary pass 3.3 s less CPU;
    the first step after a start 1.6 s less. Alone it would take a
    production pass from about 7.9 to 6.4 s.
  - Identical: the 4,520 outputs of the 1,130 recorded inputs (the real
    `step()`'s, four indicator modes) bit for bit, and three stepped replays
    (top_tier 2026-10-01 09:30-11:30, 361 steps, 20 trades; small_cap 06-02;
    zero_dte 05-20) in every event, decision, frame and dashboard digest.
    Two differences remain on frames the bot never builds: a row stamped NaT
    gets its own one-bar VWAP (NaN before), and a named column index loses
    its name.
  - Tests: `tests/market_data/test_add_indicators_pinned.py` (new) runs a
    frozen copy of the old build beside `add_indicators`, bit for bit in all
    four modes, on 23 recorded frames (36 inputs, distilled into
    `tests/fixtures/indicator_corpus/`, new), on enriched frames, and on
    winter, DST, Thanksgiving and Christmas frames with bars to 20:00 (in
    EST the evening bars fall on the next UTC date). Its 12 mutants are all
    killed; 4 of the verifier's 6 had passed the suite.
    `test_indicator_span_scale.py`: a native rebuild carries no stamp.

- **The Schwab candle parse floors every stamp to its minute at once.**
  *2026-10-05* — `MarketDataStore._history_candles_to_frame`, the parse of
  every `price_history` answer (the 1m history, the HTF frames, the daily
  bars), mapped `floor_minute` over the candles one at a time, about
  0.15 ms a candle. It now floors the whole index in one call
  (`DatetimeIndex.floor("1min")`), names it `timestamp` and drops the
  `datetime` and any `timestamp` key, as before.
  - Measured on the parse alone (Schwab-shaped answers, each regular-hours
    minute sent twice): a 1,134-candle 1m answer 167 → 6 ms, a top_tier
    15m HTF answer (10 days, 864 candles) 131 → 5 ms, a 34-candle boundary
    refresh 9.3 → 3.5 ms. So the 09:15 prewarm's parses take about 0.3 s of
    CPU instead of 8.3 s, a 15m boundary pass about 0.1 s less, and the
    09:30 stream-start backfill (every watchlist symbol's 1m history,
    fetched again inside the management window) about 4.5 s less CPU; a
    harness run without schwabdev's lock measured that pass's fetch map
    9.65 → 4.62 s and a cold start about 8.7 s sooner.
  - The frames are identical: on 5,475 archived frames (every 1m and 15m
    tape of 33 days, each as archived and Schwab-shaped with duplicate
    minutes, off-minute stamps, NaN prices and volumes, and shuffled), equal
    exactly in values, dtypes, index stamps, name and time zone, and block
    layout; a stamp in the ambiguous DST hour raises the same error. A
    stepped replay of 2026-09-23 09:58-10:50 with duplicate minutes in
    every answer (28 full HTF parses, 12 trades) was identical.
  - Tests: `tests/market_data/test_history_candle_parse.py` (new) compares
    the parse with a frozen copy of the per-candle one on Schwab-shaped
    answers (dates-mode duplicates, off-minute stamps, NaN, shuffled, both
    2026 DST changes, float stamps, no volume key, string prices, a stray
    `timestamp` key, a year of daily bars) and pins the minute floor in ET,
    the index name, the bar columns, and the per-candle parse's error for a
    stamp in the repeated fall-back hour.

- **One quote request per refresh: `runtime.quote_batch_size` is 50, in
  the code default and every preset (it was 20).** *2026-10-05* — top_tier
  quotes 28 symbols, so every engine pass refreshed them in two requests
  (20 + 8), one after the other. The second cost about 0.2 s of each 7.9 s
  pass, and the pair made up 87% of a day's Schwab calls (2026-09-29 to
  10-02, about 5,800-6,000 quote requests a day). One request now carries
  all 28.
  - Measured: the quotes phase 0.43 → 0.23 s a pass on the harness (the
    real `step()` at real-time pace, schwabdev's request lock emulated, 9
    run pairs, each 0.200-0.202 s apart). In production the second request
    took a median 0.193-0.207 s and a 20-symbol request 0.003-0.005 s more
    than an 8-symbol one, so about 0.19-0.20 s a pass: about 570-600 s of
    pass time and 2,900-3,000 fewer requests a day (-43% of the day's
    calls).
  - No decision changes. A stepped replay of 2026-10-01 09:35-11:35 (481
    steps, 25 trades) was identical, every cached quote and its refresh
    stamp included; only the quote request count halved. In real time the
    28 quotes share one fetch stamp, so the split refreshes of 09:33-09:35
    (20 and 8 symbols on alternate passes, when a pass sits just under
    `quote_cache_seconds`) are gone, and the dashboard's `api_usage` counts
    fewer quote calls.
  - A refused batch costs fewer requests (its retries are paid once, not
    once a chunk); the single-quote fallback is unchanged. Schwab served
    24-symbol requests in production (2026-05-12 and 05-13, at 25). The
    order path's quotes are one or two symbols and never split. The other
    presets' quote watchlists peaked at 4-18 symbols in the archive and
    already sent one request; small_cap's can pass 20 with held positions
    and now stays at one.
  - A deployed config that sets `quote_batch_size: 20` keeps two requests;
    set it to 50 to take the change. The knob is still an integer of at
    least 1, checked at load. README: the defaults table and the knob.
  - Tests: `tests/domain/test_config_validation.py` pins the default and
    every shipped preset at 50.

- **Clicking a symbol in the dashboard's completed trades dock switches the
  chart to it.** *2026-10-04* — it opened the symbol's TradingView page
  (user report, 2026-09-29). The cell is a button for a symbol the
  dashboard charts (for an option trade, its underlying). A symbol that has
  left the dashboard (no longer a position's, on the watchlist or the quote
  watchlist, a candidate or an S/R row) has no chart and stays plain text.
  In the stacked layout, where the chart sits above the dock, a click
  scrolls the chart's head (symbol, price) into view when it is off screen.
  - The table is rewritten only when its rows change. It was rewritten on
    every poll (1.5 s on the live config), which replaced a button under a
    click in progress, losing the click, and dropped its keyboard focus.
  - The focus events list a closed option trade under its underlying, the
    symbol its cell and its position card chart. They matched the option's
    own symbol, which no snapshot carries, so they never listed it.
  - `DashboardCache.build_payload` no longer looks up an exchange for a
    closed trade's symbol: only the TradingView links read one, and the
    watchlist cards and the selected symbol keep theirs.

- **The cycle's CPU work runs serially on the engine thread, and
  `runtime.cycle_fetch_workers` replaces `runtime.cycle_precompute_workers`
  (Stage 1c of the fast-management study).** *2026-09-28* — every step
  builds each watchlist symbol's 1m frame with its indicators (the step
  frame), then pre-warms the S/R levels and the strategy's chart, structure
  and technical contexts on those frames. The three maps ran on the same
  thread pool as the step's network fetches (the 1m history, the HTF
  refresh points and the daily-history prefetch), sized by
  `cycle_precompute_workers` (4 in every preset). The work is pandas, numpy
  and TA-Lib on small frames and holds the GIL nearly all the time, so the
  pool never overlapped it: it added CPU time and made the maps slower. The
  fetches keep their pool, but it overlaps no HTTP either (corrected
  2026-10-05; this said a pool overlaps them): schwabdev 4.0.0's
  `Client._request` holds one lock around every request, its retries
  included, so the fetches reach Schwab one at a time. The dry-run days of
  2026-09-29 to 10-02 show it: a 15m boundary's 28 HTF fetches took 5.9 to
  9.8 s (22.0 s once, while Schwab was slow), one starting about every
  0.24 s.
  - `IntradayBot._compute_symbol_map` runs a map one symbol at a time on the
    engine thread (the history-fetch decisions, the step frames, the S/R
    pre-warm and its mid-cycle rebuild, the context pre-warm);
    `_fetch_symbol_map` runs one on a pool of `cycle_fetch_workers` threads
    (the 1m history fetch, the HTF refresh points and the daily-history
    prefetch), whatever the count, and reads the results in the
    watchlist's order. Both isolate a symbol whose call raises (see Fixed).
    A stop signal (KeyboardInterrupt) while the fetch map waits drops the
    fetches not started yet, and the pool's shutdown waits only for the
    ones in flight (2026-09-29, the final review's probe): it waited for
    every queued fetch, so with 24 HTF refreshes on 4 workers in a
    price_history brownout (a 10 s timeout, GETs tried three times) the
    stop took about 180 s, past systemd's 90 s stop timeout, whose SIGKILL
    skipped the session report; the second Ctrl+C is ignored by design.
    `_parallel_symbol_map` and `_cycle_precompute_workers` are gone, and
    the step's watchlist, the HTF refresh's symbols and both maps key their
    symbols with one helper, `engine._unique_symbol_keys`. The quote
    refresh's single-quote fallbacks
    (`MarketDataStore._parallel_quote_fetch`) keep their pool, sized by
    `cycle_fetch_workers`.
  - Replayed on the harness (the real `step()` on four archived top_tier
    days, 2026-09-22 to 09-25, from 09:41, 30 steps each at real-time pace
    after two warm-up steps, network waits emulated; the two trees run one
    at a time, the order alternating by day), the means of the four days'
    medians, first against 1157622 alone: the three maps 5.39 s → 2.38 s a
    step (frames 1.70 → 0.71 s, S/R 1.30 → 0.57 s, contexts 2.39 → 1.10 s;
    2.0-2.5x by day); the span from the screener's return to the quote
    batch 5.44 → 2.50 s (2.18x; 1.95x to 2.39x by day; the median of the
    day medians, 5.36 → 2.49 s, is 2.15x); the whole step
    8.28 → 5.13 s, its CPU time 8.12 → 4.71 s. The entry pass (1.25 →
    1.19 s) and the dashboard publish (0.99 s both) did not change. The
    share of entry-pass frames a stream bar had already overtaken fell from
    6.9% to 4.2% (the four days' mean).
    Re-run on this change alone (the same harness, the tree before it
    against the tree with it, the HTF refresh points and the daily prefetch
    in both) while the machine was shared (load 19-47 against 1.3-10 for
    the first run), the means of the day medians again: the three maps
    5.14 s → 3.56 s a step (frames 1.60 → 1.11 s, S/R 1.19 → 0.83 s,
    contexts 2.35 → 1.62 s), the span from the screener's return to the
    quote batch 5.27 → 3.70 s (1.42x; 1.28x to 1.65x by day, 1.65x on the
    quietest; the median of the day medians, 5.05 → 3.84 s, is 1.32x), the
    whole step 9.82 → 7.67 s, its CPU time 8.42 → 6.97 s. The entry pass and
    the publish, which the
    change does not touch, moved 1.15x and 1.19x, so part of that is the
    load; the gain is smaller on a loaded machine, and holds on every day.
  - Production: the archived top_tier days (all on 4 workers) spent a
    median 19.3-20.0 s from the screener to the quote batch
    (`Candidate cycle` to `Quote refresh source=engine:quote_watchlist`,
    2026-09-21 to 09-25) and stepped every 24.5-27.4 s. That machine ran the
    pooled block about 3x slower than the harness and the serial phases
    1.3-2x slower (Study F), so the saving there is expected to be larger;
    the first dry-run day is the check (CYCLE_TIMING's `frame_s`, `sr_s`
    and `contexts_s` against the day before this change).
  - No decision changes. Replayed on a stepped clock (the whole session,
    09:30:30-15:59:30, a step every 29 s of replayed time; bars delivered
    between steps; the same inputs for both trees) on four archived top_tier
    days against 1157622 alone (3,220 steps: 36,132 audit events, 354
    signals, 105 trades), every audit event, entry decision, signal,
    position and trade, and every log line at INFO or above, was identical
    before and after; only the fetch pool's own `Fetching ...
    price_history` lines, which both trees write from a pool, came in
    another order at some 15-minute steps. Replayed again on this change
    alone (the tree before it against the tree with it), over 2026-09-24
    09:30:30-11:30 (248 steps, 19 trades): the same, with pool-written
    lines in another order at four steps and the HTF refresh line's
    measured seconds apart; and over the whole session of
    2026-09-25 (805 steps: 9,918 audit events, 21,063 log lines, 25
    trades, -$428.72), the same, with pool-written lines in another order
    at six steps. What changes is when: each step's decisions come sooner,
    on fresher frames, which is the point.
  - Config: `runtime.cycle_fetch_workers`, default 4, an integer of at least
    1, checked at load (`config._NUMBER_CHECKS["runtime"]`). A config that
    still sets `cycle_precompute_workers` refuses to start and names the
    replacement (`_RETIRED_SECTION_KEYS["runtime"]`); rename the key in a
    deployed config before upgrading. Every preset and `config.example.yaml`
    ship `cycle_fetch_workers: 4`, the pool the fetches ran on before, as
    do the local `configs/config.yaml` and the two microcap presets, which
    are not tracked (`tests/guards/test_preset_parity.py`). The README
    documents the knob.
  - The chart-pattern helper cache keeps its lock: the dashboard's HTTP
    thread runs chart-pattern analysis for `/api/chart` while the engine
    thread runs. The strategy's context caches (`_strategies/contexts.py`)
    keep theirs too; whether any thread but the engine's still reaches them
    was not settled here.
  - Tests: `tests/composition/test_cycle_symbol_maps.py` (new; the
    history-fetch decisions run on the engine thread, and a stop signal
    drops the fetches not started),
    `tests/composition/test_htf_refresh_points.py` (the HTF refresh and the
    daily prefetch fetch on the pool),
    `tests/domain/test_config_validation.py`,
    `tests/guards/test_shared_knob_contract.py`,
    `tests/guards/test_preset_parity.py`, and the knob's and the map's new
    names in `tests/analysis/test_silent_excepts_data_levels.py`,
    `tests/strategies/top_tier_adaptive/test_bug_regressions.py`,
    `tests/strategies/test_peer_shared_entry.py` and
    `tests/strategies/framework/test_strategy_requests.py`.

- **No read fetches HTF bars: the engine fetches them on its fetch pool, at
  the start of the cycle and again before management, the entries and the
  dashboard publish (study F, Stage 1a).** *2026-09-28* — until now the
  cycle fetched its watchlist's HTF frames at its start, when a bar had
  closed 10 s before, and every read after that settle that passed
  `allow_refresh` fetched its own symbol, one at a time: the dashboard
  publish (the S/R rows, the HTF trend, each snapshot's HTF overlays and
  level zones, while the gate refreshed market context), the adaptive
  ladder's and `sr_flip`'s S/R reads, key_levels' ladder exit, the
  strategies' entry reads (`_sr_context`, `_htf_context`, zero_dte's HTF
  trend, the entry gatekeeper's candidate snapshot) and key_levels' and
  zero_dte's entry prefetch (`prefetch_htf_contexts`). On the archived
  top_tier days 64-76% of a day's 15m fetches ran in the publish, one symbol
  at a time, and a 15m cycle took 30-53 s against the usual 20-27 s, all of
  it before the next management pass.
  - `MarketDataStore.refresh_htf_frame` is the only HTF fetch.
    `IntradayBot._refresh_htf_frames` runs it on the fetch pool
    (`runtime.cycle_fetch_workers`) for each symbol a read can ask
    for (the watchlist, the quote watchlist, the candidates, each
    position's underlying and reference symbol; the feed keeps frames for
    S/R symbols only) whose HTF bar settled (`htf_refresh_due`: the
    strategy's `htf_minutes()`, 10 s after the boundary): at the start of
    the cycle and again before management, the entries and the publish, so
    a bar that settles mid-cycle reaches the rest of the cycle at the next
    of them. Each refresh point that fetches logs `HTF refresh (<point>):
    <fetched>/<due> <tf>m frame(s) in <s>s[; failed: <symbols>]` at INFO.
  - A symbol is fetched at most once a cycle. A failed fetch is logged with
    its type (`HTF frame refresh failed for AAPL (15m): ConnectionError:
    ...`), the frame keeps its bars and stays due, and the next cycle
    retries it: in an outage one fetch per symbol per cycle, where each read
    used to retry it.
  - After a mid-cycle refresh the refreshed symbols' trading-mode S/R
    contexts are built again as the cycle's pre-warm builds them (on the
    step frame, at its close): the cycle serves every later read the build
    of its first read, which was otherwise whichever read came first.
  - The reads lose `allow_refresh` and the arguments only a read-time fetch
    used: `get_htf_frame`'s level arguments and lookback, and the
    `lookback_days` of `get_htf_context`, `get_support_resistance`, the
    strategies' `_default_htf_request` / `_symbol_htf_request` and
    `_htf_context`, and the dashboard level spec (with its
    `timeframe_minutes`: the zones read the strategy's own HTF frame, and a
    manifest's `capabilities.dashboard.level_context` naming either key is
    refused at load, naming it). Gone with them: `fetch_support_resistance`
    (the cycle's old fetch, which also built a default-mode S/R context
    only `sr_cache` held, and nothing read), `prefetch_htf_contexts`,
    `should_refresh_support_resistance`, `DashboardCache.
    snapshot_should_bypass_cache` (it rebuilt a snapshot whose read would
    have fetched) and `BaseStrategy.prefetch_entry_market_data` with its
    two overrides (below).
  - CYCLE_TIMING: the `sr_fetch` phase (the old fetch at the cycle start)
    is gone. `htf_refresh` holds the four refresh points, each entered
    where it runs and added up (a mid-cycle point's S/R rebuild included),
    and `daily_history` the daily-history prefetch (below), so neither
    lands in the `quotes`, `manage`, `entries` or `publish` phases the
    stage 1c comparison reads.
  - What changes. A cycle that starts after the settle reads the frames it
    read before: the refresh covers every symbol a read asked for, with the
    same lookback. When a bar settles mid-cycle, management, the entries
    and the publish after the next refresh point read the new bar; before,
    their S/R reads kept the cycle's older pre-warmed context and only the
    reads that fetched saw the new bar. A bar that settles during the entry
    pass or the publish reaches the next cycle, where a read-time fetch
    would have given the rest of that pass the new bar (the entry pass is
    0.7-1.9 s on production; accepted until Stage 2's tick points). The
    support_resistance-level HTF context the old fetch built as a side
    effect is built by its first reader, at that read's close.
  - Measured on study F's harness (the real step on 2026-09-24's archived
    bars, a price_history call 0.40 s, 4 workers; 9 boundaries a side, at
    10:15, 11:00 and 13:30, three start offsets each): before, the 15m cycle
    took 12.8-22.3 s against 8.4 s (21.5-22.3 s when the bar settled inside
    it and the publish fetched the 28 symbols one at a time), the management
    gap across the boundary 12.7-24.3 s (median 19.2) and the last frame
    landed 4.9-21.4 s after the settle (median 11.1). With the refresh
    points the cycle takes 12.3-15.6 s wherever the bar settles, the gap
    15.1-17.4 s (median 16.0), and the frames land 4.0-7.1 s after the
    settle (median 5.2), all fetched at a refresh point. Replayed step by
    step under a virtual clock (09-24 whole day, 09-25 to 12:30), the 1,477
    cycles in which the bar settles between cycles make the same decisions,
    signals, positions and trades as before, every HTF read seeing the same
    frame; where it settles inside a cycle no read is older than before, and
    on those two days only those cycles' skip reasons changed.
  - A decision change by design, the second of this group's with the daily
    trim (see Fixed): a signal the entries make in a cycle in which the bar
    settles reads the new bar, so its stop, target and score can move, and
    management reads it too. Over 2026-09-22 09:29:30-11:00 on the same
    harness (237 steps of 23 s, the bar settling inside the cycle) two
    steps made a different signal: PANW's SHORT
    (`top_tier_vwap_reclaim_short`) at 10:15:07 rested its stop at 371.1578
    instead of 370.5726 and its target at 360.4333 instead of 363.035 (its
    S/R hint `bearish_breakdown`, not `range_between_levels`; its runner
    target 2.06R, not 1.58R), and the position kept them to 11:00 (118
    steps), and NVDA's LONG at 10:45:01 its target at 228.2877 instead of
    228.2841. Every symbol's actions were the same, and no read was older
    (0 older, 160 newer, 25,345 equal).

- **top_tier's daily history is fetched on the fetch pool from the prewarm
  on, not inside the first entry pass, and a failed fetch is retried until
  the first entry window (study F, Stage 1b).** *2026-09-28* —
  `BaseStrategy.prefetch_entry_market_data`, a hook the entry pass called,
  is replaced by `daily_history_symbols(watchlist)`: the symbols whose daily
  history the strategy's entries read. top_tier's (small_cap_squeeze's too)
  names each watchlist symbol and its benchmark, the first of its index
  ETFs; the base names none. `IntradayBot._prefetch_daily_history` fetches
  those not fetched yet today (`MarketDataStore.daily_history_due`) on the
  fetch pool while the gate refreshes market context: from the prewarm
  (09:15 on the top_tier preset), and later for a symbol that joins the
  watchlist. Until now `_symbol_daily_stats` fetched each symbol's 180 days
  inside `entry_signals`, one at a time: on 2026-09-24 and 09-25 the 09:35
  entry pass waited about 10 s on 28 fetches, with nothing managed
  meanwhile. key_levels' and zero_dte's overrides of the old hook (a serial
  HTF prefetch inside the entry pass) are gone: the engine refreshes those
  frames before the entries. On study F's harness (09-24 and 09-25, 0.40 s
  a call) the 09:35 entry pass took 14.4 / 14.6 s (13.6 s of it the daily
  fetches), its cycle 18.9 / 19.5 s and the management gap after it 23.0 /
  24.0 s; now 2.5 s, 7.1 / 7.2 s and 11.5 / 11.8 s, and the pooled fetch
  takes 3.3-3.4 s of one prewarm cycle.
  - A fetch that raises is no longer cached as failed for the day at once:
    the prefetch fetches it again `DAILY_HISTORY_RETRY_SECONDS` (60 s) after
    it failed, cycle after cycle, until one answers or the day's first
    entry window opens (`StrategySchedule.before_first_entry`; 09:35 on the
    preset, 20 minutes after the prewarm); from then on a failure stays
    cached for the day, as before. A read
    (`get_daily_history`) never fetches a failed symbol again that day, and
    a response with no completed session is an answer, cached for the day.
    `MarketDataStore.fetch_daily_history` is the fetch (the prefetch's, and
    a read's when nothing is stored for today);
    `daily_history_due(symbol, *, retry_failed)` says which symbols are
    due. The failure's WARNING names the error type and no longer carries a
    traceback: `Daily price_history fetch failed for NVDA: ReadTimeout:
    ...; no daily stats for it until a fetch succeeds.`

- **`execution.bracket_stop_order_type` defaults to `STOP` (was
  `STOP_LIMIT`).** *2026-09-28* — a broker bracket's resting protective stop
  (`bracket_orders_enabled`, off in every preset) is a plain stop unless a
  config asks for `STOP_LIMIT`, which stays supported. A `STOP` fills
  wherever a flush ends, but it fills. A `STOP_LIMIT` whose limit the price
  gaps through triggers without filling and stays a working order, so only
  the engine's next cycle gets the position out (see **Fixed**). Study B
  (2026-09-26): on the 35 archived moved-stop exits, 25 recorded fills sat
  past the limit the old offset gave and 4 past the one an initial-R offset
  gives. `config.example.yaml`, the only preset that lists the key, sets
  `STOP`; README.md's `execution` table and **Stop order type** say so, and
  its `static` sync mode no longer says the broker owns the resting levels
  (the engine checks its own stop in every sync mode; see **Fixed**). No
  preset enables brackets, so nothing that trades changes.

- **The tests are organised by the module under test (refactor cut C48).**
  *2026-09-28* — `tests/` (source tree only; no build ships it) now has one
  directory per layer of the plan's import order: `foundation`,
  `market_data`, `analysis`, `domain`, `strategies/` (`framework`,
  `top_tier_adaptive`, `zero_dte` and the other plugins), `runtime`,
  `reporting` and `composition`, plus `guards` (the import layering, the
  plugin contract, the shared-knob contract and matrix, the preset parity
  and the S/R tolerance readers), `snapshots` and `properties`. Each file
  moves whole to the home of the module it mainly tests, under its own
  name; the names stay unique, so pytest's default import mode still
  applies. Every test reads the repo's files through
  `tests/support/paths.REPO_ROOT` instead of the working directory, so the
  suite runs from any directory (before, run from elsewhere, 2,195 tests
  failed or errored and eight parametrizations over the presets, modules
  and screeners collected nothing). The 14 files organised by the review,
  sweep or bug list that found their defects carry a registered
  `regression` marker (`-m regression` selects their 887 tests). The docs
  name the new paths: README.md, `configs/README_PRESETS.md`,
  `_strategies/README.md`, the `shared_entry` module docstring and the
  scaffolded strategy's docstring. README.md named a plugin conformance
  suite that does not exist, `tests/test_strategy_plugin_conformance.py`,
  and said it ships; it now names `tests/guards/test_plugin_contract.py`, in
  the source tree only. Two test-only cuts land with it and change no
  tracked file: C46 gives the duplicated test builders one home
  (`tests/conftest.py`'s `example_config`, `example_risk`,
  `top_tier_config` and `top_tier_candidate`; `tests/support`'s
  `make_position`, `blocking`, `entry_contexts` and `sr_level`), and C47
  gives the snapshot tests their own folder, `tests/snapshots/`, whose
  conftest keeps the generic three-way `symbol` fixture to them. The same
  7,252 tests are collected, the same 7,242 pass (the broad run leaves out
  the 10 phone-redirect end-to-end tests), from the repo root and from
  another directory, and the 10 snapshot JSONs are byte-identical.

- **`DashboardCache.symbol_snapshot` is cut into private builders (refactor
  cut C40b).** *2026-09-27* — The 650-line method now assembles the snapshot
  from builders on the same class, called in the order the data feed has
  always been read (its cycle caches are order-sensitive):
  - `_snapshot_bars`: the newest bars with their candle tags, and the spans
    of their EMAs;
  - `_snapshot_quote`: the quote block and the price the levels are read at
    (the quote's age is still read last, at the assembly);
  - `_snapshot_ladder`: the S/R ladder, nearest and next rungs each side;
  - `_snapshot_technicals`: the technical levels on the strategy's LTF
    frame, and their payload;
  - `_position_markers` (a static method): the position's chart markers;
  - `_snapshot_htf_overlays`: the HTF context and its FVGs;
  - `_snapshot_ltf_fair_value_gaps`, `_snapshot_order_blocks` (HTF and LTF)
    and `_snapshot_divergence_lines`.

  `symbol_snapshot` keeps the reads before the cache check, the level zones,
  the chart profiles, the frame's close (read once, for the LTF gaps and
  order blocks), the payload assembly and the cache store (a shallow copy on
  a hit, a deep copy on a store); `_snapshot_htf_overlays` reads the HTF
  context once, and `symbol_snapshot` hands it on to the divergence lines.
  Each builder keeps its try scope, its `log_component_failure` key and
  message and its fallback. The builders stay in `dashboard_cache.py`, where
  tests patch the module names they read (`build_technical_levels_context`,
  `fvg_payload`, `detect_per_bar_candle_patterns`). This is the split of
  `symbol_snapshot` the plan's dashboard_cache row asks for (RT-17(3)), which
  cut C40 left. Its other clause, making the FVG / order-block overlay
  builder reusable by `chart_payload`, is dropped: `chart_payload` builds no
  FVGs or order blocks (the page draws the snapshot's), so there is no second
  reader. A comment in `chart_payload` that named a line of the old method
  now says what it matches. No behaviour changes: 800 fuzzed snapshots, each
  built and then asked for twice more (from the cache, or rebuilt when a
  refresh is due), give the same payloads, the same feed and strategy reads
  in the same order and the same failure logs (key, message and exception),
  byte for byte. The cases cover every chart-profile overlay toggle, LTF 1m
  and 5m strategies, missing, stale and fresh quotes, premarket, RTH and
  after-hours clocks, S/R rows with and without a price or a spacing, equity,
  option and no positions, and a failure at each of the seven keys. Tests:
  `tests/test_silent_excepts.py` (`TestReportedSnapshotComponents`) pins the
  five failure paths no test covered (the candle tags, the technical build,
  the HTF and LTF order blocks and the divergence lines): each is logged, and
  its part falls back to empty whatever it held by then. The class's seven
  snapshot tests (these five and the two FVG ones) also check each part's
  `log_component_failure` key, which sets its once-a-minute WARNING.
  `tests/test_dashboard_cache.py` (`TestTheSnapshotHandOffs`, new) pins
  what `symbol_snapshot` hands its builders, which no test checked: the
  refresh flag reaches the HTF context read, after hours the close and the
  percent change are the candidate row's, and the technical levels are
  built at the fresh quote's last. It also pins that a stale quote gives no
  last, bid or ask, and that the payload built on a cache miss is stored as
  a deep copy, so a caller's change to that payload does not reach the next
  cache hit. A hit hands out a shallow copy, by design.

- **One screener template per family; the candidate ranking, the gap score
  and the anti-chase reason names each live once (refactor cut C45).**
  *2026-09-27* — Screener and plugin code that was written out several times
  now has one home:
  - The peer screeners run one algorithm.
    `PeerConfirmedKeyLevelsScreener.run` is the template: the curated query,
    the per-symbol metadata, the missing-symbol warning, the effective RVOL
    and RVOL profile, the sort and the ranks. key_levels_1m still screens
    under its own name; `_active_strategy_name` loses its `config.strategy`
    fallback, which was never reached because the base class rejects an
    empty `strategy_name`. `peer_confirmed_trend_continuation` and
    `peer_confirmed_htf_pivots` now subclass
    `PeerConfirmedKeyLevelsScreener`; each had its own copy of `run`. They
    override only its hooks:
    - `_relative_volume_cap(params)`: htf_pivots reads its
      `screener_relative_volume_cap`.
    - `_score_row(day_change, effective_relative_volume, params)`: the
      activity score, the directional bias and the metadata that explains
      them. key_levels scores |move| x RVOL with no bias; trend_continuation
      scores the same with a +/-0.30% trend bias; htf_pivots scores the move
      fit x RVOL with a contrarian bias.
    - `_sort_key`: htf_pivots breaks a score tie on the move fit, the others
      on the size of the move.
  - `screener_base.rank_candidates(candidates, limit=None)` puts the highest
    activity score first, breaks ties by the screener query's own order,
    cuts to `limit` and renumbers the ranks 1..N.
    `TradingViewScreenerClient._candidate_rows` and small_cap_squeeze's
    premarket-lock merge each had a copy. It lives in `screener_base`, not
    `screener_client`: a plugin may not import the runtime layer.
  - `screener_base.gap_rvol_activity(row)` is the gap-and-go score (change
    from open x RVOL clipped to [0.5, 3.0]). It replaces the lambda written
    out in the opening_range_breakout, microcap_gap_orb,
    microcap_pm_breakout and small_cap_squeeze screeners.
  - `BaseStrategyScreener._premarket_locked_candidates(now, cached,
    last_refresh, label)` is the ORB family's premarket watchlist
    (`orb_watchlist_mode: premarket`), which opening_range_breakout and
    microcap_gap_orb each carried. Before 09:30 ET it screens. From the open
    it returns the same day's list; with none it returns no candidates and a
    warning.
  - `MicrocapGapOrbScreener.require_change_filter` (on) gates on `change` as
    well as `change_from_open`. microcap_pm_breakout turns it off instead of
    repeating gap_orb's `run`.
  - `strategy_base.EXHAUSTION_REASONS` names, per side, the reasons
    `_entry_exhaustion_reasons` (the anti-chase checks) emits; it sits above
    `BaseStrategy`, beside its emitter. The FVG retest-deferrable sets of
    opening_range_breakout and momentum_close (the LONG entry),
    rth_trend_pullback and volatility_squeeze_breakout are now their own
    reasons plus it. A renamed or added check therefore reaches all four;
    before, each set listed the four names by hand.

  `_strategies/README.md` names the shared code under "What lives where" and
  tells a plugin whose retest may clear the anti-chase checks to add
  `EXHAUSTION_REASONS[side]` to its deferrable set. There is no alias; the
  private copies are gone. No behaviour changes: the screen queries, the
  candidates (rank, score, bias, metadata), the warnings' text, the
  deferrable sets and the entry decisions of the five breakout strategies
  are identical before and after. This was checked on 3,542 randomized and
  recorded-tape cases, among them the recorded AAPL / SPY / TSLA sessions
  every 10 minutes with the exhaustion checks tightened so the retest
  deferral decides. Two log lines, their text unchanged, now carry the
  logger of the module that holds their code:
  - the peer missing-symbol warning, from
    `peer_confirmed_key_levels.screener` for trend_continuation and
    htf_pivots too;
  - the premarket-watchlist warning, from `_strategies.screener_base`.

  htf_pivots' `screener_bias_mode` candidate metadata now sits with its
  other score keys; nothing reads the metadata's key order. closing_reversal
  and mean_reversion stay separate plugins. They differ in 14 places across
  their entry loop (reference high, bounce check, chart predicate, target,
  reason, score, three defaults), so a shared base would be mostly hooks.
  The README's example screener and the plugin scaffold's screener template
  read the screener's own params (`config.strategies[strategy_name]`), as
  every shipped screener does, and no longer the running strategy's
  (`config.active_strategy`). Tests: `tests/test_screener_templates.py`
  (new), `tests/test_screener_regressions.py` (the example and the template
  read their own params).

- **The 0DTE regime and option-chain plumbing have their own modules
  (refactor cut C44).** *2026-09-27* — `zero_dte_etf_options/regime.py`
  (`RegimeMixin`) holds `_regime_confirm`, which is no longer one 465-line
  method. It runs as stages: `_underlying_tape`, `_confirm_index_tape`,
  `_vix_read`, `_regime_gate_reasons`, `_regime_contexts`,
  `_htf_trend_confirmation`, `_fvg_regime_scores`, `_regime_scores`,
  `_select_regime`, `_regime_vetoes` and `_regime_metrics`. Each stage is
  the method's own block, unchanged, passing frozen records
  (`_UnderlyingTape`, `_IndexTape`, `_RegimeContexts`, `_HtfTrend`). The
  module also holds the static tape stats (`_safe_pct`,
  `_fraction_relative`, `_flip_count`, `_recent_range_pct`),
  `_htf_trend_context` and `_ambiguous_regime_reason`, whose only reader is
  the regime. `zero_dte_etf_options/chain.py` (`OptionChainMixin`) holds the
  chain cache, `_fetch_raw_option_chain`, `_fetch_filtered_contracts`,
  `_prefetch_option_chains`, the vertical and single-option validators with
  their failure details, and the quote-stability loop (`_stabilize_quotes`).
  `ZeroDteEtfOptionsStrategy(OptionChainMixin, RegimeMixin, BaseStrategy)`
  keeps the style table and the entry loop, the builders, the entry gates
  (`_underlying_below_min_price`, `_admit_premium_entry`) and the dashboard
  hooks (`live_activity_score` and the rest, which the dashboard
  duck-types). Its `__init__` still creates the chain cache state. There is
  no alias: `_ambiguous_regime_reason` imports from
  `zero_dte_etf_options.regime`. The two chain warnings ("Option chain read
  failed", "Option chain prefetch failed") now log under
  `..._strategies.zero_dte_etf_options.chain`. The knob-contract scans
  (`tests/test_shared_knob_contract.py`) read `regime.py` and `chain.py`
  too, and the HTF EMA-span scan (`tests/test_htf_ema_knobs.py`) reads the
  whole package, checking that it found `strategy.py`, `regime.py` and
  `chain.py`. No behaviour changes: on the B11 entry's replay (34
  tape-days, 4,556 checkpoints), every probe is identical before and after
  the split, 3,195,450 compared values in all. The probes are the regime
  (its scores, metrics and contexts field by field), every entry run with
  its signals, decisions and ordered feed calls, the tape stats, the chain
  cache against a scripted client and clock, and the validators and the
  stabiliser on direct inputs. The prefetch's thread order is compared as a
  set. Tests: `tests/test_zero_dte_chain.py` (new: the chain-read tests,
  `TestZeroDteChainRead`, moved from `tests/test_schwab_api.py` with the
  code they test; their scripted client and response are
  `tests.support.brokers._ScriptedClient` / `_schwab_response`; the two
  chain warnings are read under the new logger),
  `tests/test_zero_dte_shared_entry.py` (`TestTheRegime`: `_UnderlyingTape`
  and `_IndexTape` carry each read of their tape, and `regime['metrics']`
  the records' reads, on tapes where each read has its own value; the
  index is read ranging, bullish, bearish and short of its bars),
  `tests/test_zero_dte_entry_styles.py` (the declaration guard reads the
  regime and chain mixins too: both manifests declare every param they
  read), `tests/test_plugin_contract.py` (the name-disjointness scan knows
  the two mixins) and `tests/test_numeric.py` (the import). The screener's
  docstring, the plugin scaffold's template and both READMEs
  (`_strategies/README.md`, the strategy's own) name the new modules.

- **One opening-range computation, `bars.opening_range` (refactor cut
  B10).** *2026-09-27* — `bars.opening_range(frame, day, *, start, minutes,
  min_bars)` returns `(high, low, bars)` over the bars of `day` labelled in
  the half-open window `[start, start + minutes)`, or None when the window
  holds fewer than `min_bars` bars or its high or low is NaN. It replaces
  three copies, each with its own window convention and bar rule:
  `opening_range_breakout` (and `microcap_gap_orb`, which inherits it)
  sliced the day and masked the window by hand; top_tier_adaptive's
  `_opening_range` mapped a lambda over the day's bars; and the 0DTE
  strategies' `_opening_range` (one method since cut B11) read their
  configured window with `between_time`, closed at both ends. Each caller
  keeps its own start, clamp and bar rule:
  - the ORB strategy: 09:30, `max(0, opening_range_minutes)`, one bar;
  - top_tier (`regimes/orb.py`): 09:30, `_orb_range_minutes()`
    (`max(1, orb_range_minutes)`, in `schedule.py`, the clamp
    `_orb_range_end` now reads too), one bar. `_opening_range` returns the
    helper's `(high, low, bars)` or None, not `(None, None)`;
  - both 0DTE strategies: `_opening_window()`, the start
    `orb_opening_window_start` and the minutes through
    `orb_opening_window_end` inclusive (the knobs name the range's first and
    last 1m bar, as the closed window read them), `orb_opening_min_bars`.
    `_opening_range` returns the helper's `(high, low, bars)` or None.

  On 1m bars labelled at the minute the closed and the half-open windows
  select the same bars.

  **Behaviour changes:**
  - The ORB strategy no longer requires `opening_range_minutes + 2` bars of
    the day. That count included premarket bars, so it did not measure the
    opening range; the range needs a bar, and the trade a bar after it, as
    before. On a tape without premarket bars the 09:35 break of a 5-minute
    range waited a bar (6 < 7). On the 724 archived symbol-days (33
    sessions of recorded 1m bars), at the shipped 5 minutes and every minute
    of the 09:37-10:05 entry window, the decision changes on 7 symbol-days,
    all thin prints (ABTS, ANY, XOS and ZJYL between 2026-05-29 and
    2026-06-02, CTVA on 2026-05-28, the VIX index on 2026-04-30 and
    2026-05-18) with at most 2 premarket bars: on 6 of them the setup is now
    evaluated where it used to be skipped as `opening_range_incomplete` (35
    of 20,996 checks, up to 21 minutes earlier on ABTS), and on ZJYL (29
    checks) and in 10 other checks it is still skipped as incomplete with
    the window's own counts in the reason. A symbol that is now evaluated
    and refused records the setup's reasons instead, so a divergence-only
    entry (`use_divergence_entry_signal`) may consider it. Replayed through
    both ORB strategies' `entry_signals` on the first six sessions and the
    small-cap days, with and without their premarket bars, 5 of the new
    evaluations per strategy build a signal, all on the tapes without
    premarket bars.
  - A range with no price (every high or every low in it NaN) is no range.
    The ORB strategy reported it as `opening_range_values_nan`; it is now
    `opening_range_incomplete(required>=1,current=0,...)`, and the old token
    left `shared_entry.DIVERGENCE_INELIGIBLE_REASONS` (the new one is in
    it). top_tier let a NaN edge through its `or_high <= or_low` check and
    the ORB builder carried it on to a NaN target or stop; the 0DTE
    strategies read it as 0.0, which any close breaks bullishly, so either
    one built a bull ORB entry on it. The feed's frames never hold such a
    bar (`ensure_ohlcv_frame` drops a bar without a price): top_tier and
    both 0DTE strategies return the same range on every archived
    symbol-day, at every clock from 09:25 to 10:35 and at every setting
    tried (top_tier 0-30 minutes; the 0DTE window 09:30-09:34, 09:30 alone,
    09:31-09:40 and 09:30-09:59 with 0-5 bars), with and without the
    premarket bars, and their entry decisions are the same.
  - Both 0DTE strategies fail at load when `orb_opening_window_end` is
    before `orb_opening_window_start` (the base's check, since both read
    the window): pandas' `between_time` read the reversed pair as the bars
    outside it, a range of premarket and afternoon prints. A one-bar window
    (the same start and end) builds. No preset is affected.

  Tests: `tests/test_bars.py` (`TestOpeningRange`, new),
  `tests/test_orb_regime.py` (a range without a price; the one-source test
  reads the bar count and clamps a 0-minute range),
  `tests/test_breakout_conversions.py` (the 09:35 break with no premarket
  bars; a range of one bar, which both ORB strategies still read; the NaN
  reason; a range with no bar after it, whose reason carries the range's
  own bar count), `tests/test_zero_dte_entry_styles.py` (the window's last
  bar and a range without a price, both presets),
  `tests/test_strategy_time_params.py` (the reversed window, ending one
  minute and four minutes before its start, both presets),
  `tests/test_top_tier_adaptive_new_regimes.py`. The NaN, the
  reversed-window and the ORB-break cases fail on the old code; the
  window's last bar pins what the change must keep.

- **The 0DTE strategies run one entry loop (refactor cut B11).**
  *2026-09-27* — `ZeroDteEtfOptionsStrategy.entry_signals` runs the
  strategy's style table, `_entry_styles()`. Each row is an `_EntryStyle`:
  the `options.styles` token; its kind (`orb`, `trend` or `credit`), which
  sets its window (`<kind>_start_time` to `<kind>_end_time`), the regime it
  trades and its trigger; its builder; and its own blockers, which ride on
  the premium proposal as pending reasons. A kind the loop does not know is
  refused when the row is built. zero_dte_etf_long_options no longer carries
  a copy of the loop: it overrides the table (`orb_long_option`, then
  `trend_long_option` with `_long_option_style_gate`) and sets
  `_PREFETCH_OPTION_CHAINS = False`, so the parallel chain prefetch stays
  the spreads' alone, as before. The loop's steps are methods
  (`_style_window`, `_opening_range`, `_trend_momentum_blocker`), and every
  builder takes `pending_reasons`. Two dead pieces are gone:
  `no_contract_selected`, which could not be reached (a style that tried to
  build always records why it did not), and the long options' `.get` reads
  of the last close / VWAP / ret5 (the regime reads the same columns with
  `[]` first). The loop's code defaults are the spreads' (35 bars, 13:30
  cutoff, `trend_min_ret5` 0.0007, trend window to 13:40), and both
  manifests declare every param the loop reads, so no code default decides.
  The long options' 90 / 13:45 / 0.0006 were fallbacks only. Their manifest
  and preset now also declare `credit_activity_min` / `credit_activity_max`
  at 0.80 / 1.30, the code defaults their inherited range score read
  undeclared. The quote-stability checks are one loop,
  `_stabilize_quotes(data, legs, *, validate, failure_detail, source)`,
  returning the re-quoted legs and None, or None and why. It replaces
  `_stabilize_spread_quotes_detailed`, the `_stabilize_spread_quotes`
  wrapper and `_stabilize_single_option_quote`.
  `_single_option_market_failure_detail` describes a refused single option
  as `_spread_market_failure_detail` describes a vertical. The 0DTE family
  is a class constant, `_OPTION_FAMILY`; until now the base strategy named
  its subclass in two literals. The constant is deliberately not the
  catalogue's `is_option_strategy`, which would take in any future option
  plugin.

  **Behaviour changes:**
  - The debit ORB (`orb_debit_spread`) takes the long options' opening
    range. That is the bars from `orb_opening_window_start` to
    `orb_opening_window_end` (09:30 and 09:34, both inclusive), once at
    least `orb_opening_min_bars` (3) of them are in. The ORB fires from
    `orb_start_time` (09:35). zero_dte_etf_options' manifest and preset now
    declare all four, and `time_params` checks the three times at build.
    Before, the debit ORB took any bar of a fixed 09:30-09:34 window, so a
    lone 09:34 bar (a late or gappy feed) was its opening range, the case
    the long options' own copy had guarded since 2026-05-14. On the real
    session archive, every SPY / QQQ day (20) had all five opening bars.
    Across all 730 archived and fixture tapes, only three small-cap days had
    fewer than three, so the change applies to feed gaps only. The replay
    below also reran the ORB window with only one or two of the five
    opening bars in the frame (4,136 debit-spread runs, the regime forced to
    either trend). The old loop tried the ORB on that range in 992 of them
    and built a signal in 810; the new one tries it in none. With three or
    five bars in, only quote details differ.
  - The debit spreads and the long options record why the quotes did not
    settle: `quote_not_stable(reason=quote_not_fresh|missing_leg_quotes|
    mid_drift_too_high|<the validator's refusal>,...)`, where they recorded
    a bare `quote_not_stable`. The reports bucket skips by the head, which
    has not changed. The decision log skips a repeat of the same reasons
    within 120 s, so a refusal whose detail numbers change from cycle to
    cycle is logged each cycle (a TRADEFLOW Decision line and a SKIP_SUMMARY
    event), where the bare reason was logged once per 120 s. Every other
    detailed reason is already logged this way. The dashboard's compact
    decision label (the focus line beside the symbol) looks a token's short
    label up by its name, so it still reads "quote unstable"; a reason with
    no short label shows as before. Over the decision reasons of the
    archived sessions, the only other one that reads differently is
    `iv_rank_too_low(...)`, now "iv low" like the bare token. The credit
    spread's `midday_credit_spread_unavailable(reason=...)` is unchanged. No
    decision changes.
  - zero_dte_etf_long_options now skips an underlying the spreads hold
    (`underlying_already_open`), as the spreads already skipped one it
    holds. It also marks a spread's vertical (`position_mark_price`) instead
    of leaving it to the position manager's quote snapshot. This only
    matters with both strategies' positions open, and no current path puts
    them side by side: the engine runs one strategy, option positions are
    not restored at startup, and a restored position takes the active
    strategy's name. No archived session had both.

  Replay (read-only over the session archive):
  - Tapes: 34 tape-days, which are the archive's SPY / QQQ days (16 of them
    with their confirmation index and VIX) and the fixture tapes.
  - Clocks: 67 a day (every minute 09:30-10:10, every ten minutes to
    14:30), 2,278 checkpoints per preset.
  - Runs at each checkpoint: the strategy's own regime; the other strategy
    holding the underlying; and each regime forced under stable quotes and
    under one failing kind (stale, a leg missing, or a drifting mid). That
    makes 53,312 entry runs, with a synthetic chain around the close.
  - Result: with the strategy's own regime (4,556 runs) there is one
    difference: a debit spread's `quote_not_stable` now carries its detail
    (`net_mid_too_low`). No signal changes. Every other difference comes
    from the three changes above:
    - 486 quote details;
    - the long options skipping an underlying the spreads hold, 2,108
      runs, 9 of which had signalled;
    - the ORB gap runs;
    - the `no_style_trigger` reason's `or_high` / `or_low` reading `na`
      while fewer than three opening bars are in: 3,552 runs, at
      09:31-09:32 before the ORB window opens, and in gap runs with no
      breakout.

  Tests: `tests/test_zero_dte_entry_styles.py` (new; the ORB's VWAP hold
  is pinned for a bull and a bear break) and
  `tests/test_zero_dte_quote_stability.py` (new),
  `tests/test_strategy_time_params.py`,
  `tests/test_zero_dte_shared_entry.py` (the `reasons`-list scan reads the
  one loop) and `tests/support/factories.py` (the wired strategies patch
  `_stabilize_quotes`).

- **top_tier_adaptive's engine is split across its package (refactor cut
  C43).** *2026-09-27* — `top_tier_adaptive/strategy.py` (4,595 lines down
  to 1,888) keeps the class (`__init__`, the HTF EMA hooks,
  `dashboard_htf_trend`, `active_watchlist`, `_build_ladder_rungs`,
  `required_history_bars`, `should_force_flatten`), `_breakout_reference`,
  `_finalize_signal`, the entry loop and the regime-family constants only
  the loop reads. The rest moved, unchanged, into mixins of
  `TopTierAdaptiveStrategy` beside it: `schedule.py` (`ScheduleMixin`: the
  ORB window and its load check `_validate_orb_window`, `_allowed_regimes`
  and the extended-hours set), `confirmation.py` (`ConfirmationMixin`:
  sector confirmation, daily statistics, the score normalisation with
  `REGIME_SCORE_CEILINGS`, the side asymmetry, the side vote, the
  confirmation bar, the live bias and stop widening), `armed_retest.py`
  (`ArmedRetestMixin`: `ARMED_RETEST_REGIMES` and the armed retest) and
  `regimes/{trend,orb,pullback,range,vol_squeeze,momentum,vwap_reclaim,sr_scalp}.py`,
  each regime's scorer and builder with their helpers. The 54 moved methods
  are byte for byte the same. `entry_signals` is cut along its passes into
  `_read_candidate`, `_score_sides`, `_queue_builds`, `_run_build_queue` and
  `_record_candidate`, sharing two private dataclasses (`_EntryCycle`,
  `_CandidateRead`); each stage is the old text one level less indented, a
  candidate-level `continue` is a `return None`, and the comments that
  pointed "above" or "below" across a stage name it. Every `self._x` call,
  `SmallCapSqueezeStrategy` (unchanged) and every test patch on the class
  keep working. There is no alias: import `REGIME_SCORE_CEILINGS` from
  `top_tier_adaptive.confirmation` and `ARMED_RETEST_REGIMES` from
  `top_tier_adaptive.armed_retest`. `BaseStrategy.__init_subclass__` now
  looks a reserved name up on the class (`hasattr`), so a strategy cannot
  carry one in a mixin either; `BaseStrategy` and `ContextBuildersMixin`
  define none, so every shipped class loads as before. The source scans read
  the new modules too, not just `strategy.py`: `test_shared_knob_contract`'s
  rules (c) and (e) every module of a plugin but its screener and
  `__init__`, the param-declaration check every module of the engine, and
  the regime-flag and removed-param scans every module of
  `top_tier_adaptive/`. No behaviour changes: on four archived top_tier
  sessions, two small-cap sessions and the fixture tapes, under the shipped
  presets and several wider variants, every signal, entry decision and piece
  of cross-cycle state, every moved method called directly and the schedule
  grids are the same before and after the cut (163,236 records, among them
  465 signals over seven regimes, 86 distinct decision reasons and 580
  armed-retest `enter` verdicts). Tests: `tests/test_plugin_contract.py`
  (`TestEachStrategyNameHasOneHome`, new: no two mixins of a strategy, and
  no mixin and `BaseStrategy`, define the same name, since the MRO would
  pick one of the two without a word, and the scan sees each strategy's
  mixins), `tests/test_shared_knob_contract.py` (the scans read the modules
  beside `strategy.py`; a reserved name in a mixin raises),
  `tests/test_param_declaration_drift.py` (the scan reads every engine
  module), `tests/test_skip_decision_regime.py` and
  `tests/test_last_bar_atr.py` (the stages that now hold the reads),
  `tests/test_top_tier_megacap.py` (`TestTheCandidateReadHandOff`, new: the
  side vote, its breakdown, the widening factor and the index read each
  reach the stage that reads them, which no other test checked; the vote
  test gives a LONG trend a qualifying score, so the refusal it checks has
  to happen; and `TestTheSharedEntryStage`: `_queue_builds` orders the
  queue on the normalised score, not the raw one or the side, so a SHORT
  range at 4.95, 0.9 normalised, is tried before a LONG vol_squeeze at
  6.0, 0.8 normalised), and the constants' imports in the regime tests and
  `tests/support/factories.py`.

- **The strategy's context builders have their own module,
  `_strategies/contexts.py` (refactor cut C42).** *2026-09-27* —
  `ContextBuildersMixin`, which `BaseStrategy` inherits, holds every
  analysis context a strategy reads: the chart-pattern and candle contexts;
  the S/R and HTF contexts, with the HTF EMA trend (`_htf_bias`,
  `_htf_ema_alignment_sides`, `_side_vote_edge`) and the timeframes they are
  built on (`htf_minutes()`, `htf_lookback_days()`, `ltf_minutes()`,
  `_is_ltf_token`); the LTF fair value gaps; the LTF and HTF order blocks;
  `_resampled_frame`; market structure; the technical levels; the requests
  the builders pass (`htf_fvg_request()`, `ltf_fvg_request()`,
  `order_block_request()`, `_default_htf_request()`) and the score context
  built on the last (`_default_htf_context_for_score`); their `*_lists`
  flatteners; and the dashboard's reads of them (`dashboard_candle_context`,
  `dashboard_htf_trend`, `dashboard_htf_ema_columns`). The per-cycle candle,
  chart, structure and technical caches, their locks and the engine's
  pre-warm moved with them (`_observed_contexts`, `set_prewarm_frames`,
  `_observe_context`, `prime_cycle_contexts`, `reset_context_caches`), and
  so did the candle cache's reset: `_reset_entry_decisions` calls the
  mixin's `_reset_candle_context_cache` where it emptied the cache itself.
  `BaseStrategy.__init__` builds the caches through `super().__init__()`,
  where it built them, and the mixin's `__init_subclass__` gives each
  strategy class its own `_observed_contexts`, as `BaseStrategy`'s did (the
  engine's pre-warm reads it with no fallback).
  `strategy_base.py` keeps the plugin contract, the watchlist and dashboard
  hooks, the settings accessors, force-flatten, decision recording,
  `_direction_token`, `_entry_exhaustion_reasons`, the structure-event
  reads, trade management and the hooks (2,072 lines down to 1,113;
  `contexts.py` is 1,010). The 38 methods moved verbatim under their own
  names, so no call site changes (`self._structure_context(...)`,
  `strategy.htf_minutes()` and `BaseStrategy._sr_lists(...)` resolve through
  inheritance); the one edit inside the moved text is `_sr_lists`' static
  call, `ContextBuildersMixin._structure_lists(...)` for
  `BaseStrategy._structure_lists(...)`. There is no alias. A test that
  patches a builder's collaborator now patches it on `_strategies.contexts`
  (`analyze_market_structure`, `build_technical_levels_context`, and the
  `htf_ema_spans` that `_default_htf_request` reads: `strategy_base` still
  imports it for `__init__`'s span check and `dashboard_level_context_spec`,
  so a patch there no longer reaches the builder), and the tests'
  `_StrategyStub` borrows its six timeframe and request reads from
  `ContextBuildersMixin`. The builders' four DEBUG lines, for a cached FVG,
  order-block or merged-frame read that raised and fell back to the frame,
  now log under `intraday_tv_schwab_bot._strategies.contexts`. No behaviour
  changes: all 17 presets, each as shipped and with every optional context
  switched on, ran over the fixture tapes before and after the move (each
  moved builder, flattener, request and hook at 56 clocks on four symbols,
  with a feed, with no feed and with a feed whose cached reads raise; the
  pre-warm cycle as the engine drives it; `entry_signals`, the recorded
  entry decisions and the shared exit decisions at 121 clocks: 4,114
  cycles, 22,748 decisions, 132 signals and 1,110 exit decisions), and the
  68 output files are byte for byte the same. Tests: `tests/test_contexts.py`
  (new: each strategy class keeps its own pre-warm record, a build records
  on the strategy's own class, each instance keeps its own caches and locks,
  and each entry cycle empties the candle cache and the engine the
  pre-warmed ones); the retargeted patches in test_sr_tolerance_reads,
  test_shared_entry_policy, test_fix_strategy_consumers and
  test_htf_ema_knobs; `tests/test_module_layering.py`'s strategy-framework
  layer gains `_strategies.contexts`, which the runtime and composition
  layers, as with `strategy_base`, may import only under `TYPE_CHECKING`.

- **Each `options.underlyings` entry is checked at load, and the dashboard
  publishes the strategy's own tradable and index symbols (refactor cut
  B16).** *2026-09-27* — the decided half of the cut had landed: a scalar
  `options.underlyings` fails at load (cut C24), and the dashboard's copies of
  the strategy's symbol reads went on 2026-09-26. Every entry of the list must
  now be one ticker the symbol normalizer keeps, with no whitespace, comma
  or semicolon inside it (`SPY QQQ`, `SPY,QQQ` and `SPY;QQQ` are two
  tickers; `$SPX`, `BRK.B` and `BRK/B` one each): each one that is not fails
  at load naming it, `options.underlyings[INDEX] must be a ticker, got ...`,
  in the event rows' message format and with the quote hint they and
  `runtime.startup_reconcile_ignore_symbols` give for a ticker YAML read as
  a boolean or null. The rule is stricter than theirs, which asks only for a
  non-blank string (`NONE` and `QQQ IWM` pass there). The list the bot reads
  is the checked one, upper case, stripped and each ticker once;
  `_normalize_options_config` no longer touches it, since it kept the raw
  list whenever nothing of it survived normalizing. `null` still loads as no
  list, which only a stock strategy accepts.
  `DashboardCache.tradable_symbols` and `index_symbols` are gone: they
  normalized the hooks' already normalized lists again, and
  `DashboardCache.build_payload` publishes `dashboard_tradable_symbols()` /
  `dashboard_index_symbols()` as the strategy returns them (no plugin
  overrides either; 24 published engine states read byte for byte as
  before).

  **Behaviour change:** a config that loaded before may now refuse to start,
  naming the entry. Until now `[SPY, ON]` loaded as `['SPY', 'TRUE']` (YAML
  reads an unquoted ON as true), so ON Semiconductor was traded as the ticker
  TRUE; `[~]`, `[NONE]` and `[' ']` loaded as the raw list, which passed the
  emptiness check: an options strategy started and its screener offered `NONE`
  as a ticker, or nothing, while the options position cap counted the raw
  entries; `[SPY, ~]` loaded as `['SPY']` without a word, and `['QQQ IWM']`,
  `['SPY,QQQ']`, `['SPY;QQQ']` and `[5]` as the tickers `QQQ IWM`,
  `SPY,QQQ`, `SPY;QQQ` and `5`. Quote a ticker YAML would read as a boolean
  (`'ON'`), and give each ticker an entry of its own. Every shipped preset
  loads unchanged (`[SPY, QQQ]`). Tests: `tests/test_config_validation.py`
  (`TestOptionsValidation`: each bad entry named with the hint, two tickers
  in one entry joined by whitespace, a comma or a semicolon, `$SPX`, `BRK.B`
  and `BRK/B` loading as one ticker each, no options strategy loads with no
  underlyings, the normalized list), `tests/test_index_symbols.py`
  (`TestTheDashboard`, read from the published state) and
  `tests/test_silent_excepts.py` (`TestRemovedHandlers`).

- **A symbol's S/R snapshot has its own module, `sr_snapshot.py`, and the
  position manager no longer imports the dashboard (refactor cut C41).**
  *2026-09-27* — `sr_snapshot.sr_snapshot(config, data, symbol, *, price,
  strategy, account, allow_refresh)` is what `DashboardCache.sr_row` built:
  the trading-mode S/R context, its state (breakout, near_support, ...), the
  HTF trend (the strategy's own read, else the generic 50/200 one, with the
  strategy's HTF FVG request), the market-structure bias and last event, and
  the level prices. It reads the strategy's timeframes itself
  (`strategy.htf_minutes()` / `htf_lookback_days()`, cut C32), so there is
  one resolution and no timeframe arguments. `DashboardCache.sr_row` is the
  snapshot plus the strategy's LTF label (`ltf_timeframe`), third in the row
  as before. The exit record (`PositionManager._position_exit_context`)
  reads the snapshot itself, so `PositionManager` loses its
  `dashboard_cache` argument and its one import of a dashboard module is
  gone (the layering guard puts both in the runtime layer, so no layer rule
  changes). `DashboardCache`'s `symbol_price` (the price when none is given,
  over `DISPLAY_PRICE_KEYS`) and `htf_trend` moved with it
  (`sr_snapshot.symbol_price`, and the private `_htf_trend`), and
  `dashboard_structure_event_label` is `sr_snapshot.structure_event_label`;
  the moved code's guards against a missing data store, config section or
  account are gone, since all three are required arguments. The exit record
  still reads the snapshot inside its own exception boundary, since the
  record is built before the exit order goes out; a failure is logged by the
  position manager's own `ComponentFailureLog`, at WARNING at most once a
  minute and DEBUG in between, as it was through the dashboard's (the plan's
  DEBUG-only line would have lowered it). There is no alias. No behaviour
  changes: 2,000 fuzzed S/R rows, 1,000 exit records, 200 snapshots and
  charts and 24 published engine states read byte for byte as before. Tests:
  `tests/test_sr_snapshot.py` (new: the classification, the strategy's
  timeframes and HTF FVG request and the caller's `allow_refresh` on every
  read, the price fallback from the quote to the 1m close to the account,
  the strategy's trend, the row's key order, an option's exit record read on
  its underlying without a refresh, the structure-event label, and that
  neither reader imports a dashboard module), `tests/test_silent_excepts.py`
  (`TestReportedExitContextSrRow`, now also the DEBUG line within the
  minute), `tests/test_quote_price_reads.py`,
  `tests/test_strategy_requests.py`, and the manager stubs in
  `tests/support/brokers.py` and test_option_same_level_block;
  `tests/test_module_layering.py`'s runtime layer gains `sr_snapshot`.

- **The dashboard's payload helpers, level zones and state build have homes
  of their own (refactor cut C40).** *2026-09-27* — `dashboard_payloads.py`
  holds the stateless helpers that were `dashboard_cache` module functions,
  without their `dashboard_` prefix: `normalize_exchange`, `quote_exchange`,
  `technical_line_payload`, `fvg_payload`, `fvg_anchor_abs_index`,
  `cache_json_signature`, `frame_signature`, `recent_trade_markers`,
  `symbol_trade_signature`, `bars_from_frame` and `htf_chart_frame`.
  `dashboard_zones.py` holds the second half of
  `DashboardCache.strategy_level_zones`, which was nested closures:
  `level_anchors`, and `build_level_zones(candidate_zones, *, close,
  flip_frame, flip_confirmation_bars, timeframe_minutes)`, which gives each
  zone its flip state, merges a kind's zones at one price, trims an
  overlapping support and resistance and picks the zones drawn. The method
  keeps the reads (the spec, the HTF context, the LTF frame and its ATR, the
  strategy's hooks and the zone each candidate makes) and hands its zones
  over. The symbol part of the dashboard state is
  `DashboardCache.build_payload(positions=, last_candidates=, watchlist=,
  quote_watchlist=, entry_decisions=, warmup_summary=, allow_refresh=)`: the
  performance with its positions' S/R fields, the candidates card, the
  snapshots and exchanges, the symbol lists, the chart settings and the
  cache prune (cut C34's `prune_inactive_symbols` call). The engine's
  `_dashboard_state` reads the warmup summary on the error path (now before
  the build's S/R rows, where it read it after them; the summary reads no
  HTF state, so only its clock fields, `retry_delay_seconds` and
  `last_stream_bar_age_seconds`, can differ, by the time the rows take),
  hands the build the engine's state, adds its own status fields and
  publishes them in the order they had; `_publish_state` still isolates a
  failed build and marks the page stale. The engine imports only
  `DashboardCache` from `dashboard_cache`, where it took the two exchange
  helpers too, and nothing from `dashboard_payloads` or `dashboard_zones`.
  The rate-limited failure log is `log_setup.ComponentFailureLog` (WARNING
  with the traceback at most once a minute per component, DEBUG in between),
  which `DashboardCache` keeps as `log_component_failure`, so its call sites
  are unchanged. `DashboardCache` requires its strategy, feed and account
  (keyword arguments with no default): every builder reads the strategy, and
  `build_payload` and `symbol_snapshot` read the feed and the account
  unconditionally, so the `strategy is None` answers of `candidate_limit`
  (and its unused `strategy` argument), `tradable_symbols`, `index_symbols`
  and `strategy_level_zones`, the `data is None` checks of
  `strategy_level_zones` and of the snapshot's overlay reads, and the trade
  markers' and trade signature's reads of an account without `trades`
  (`recent_trade_markers`, `symbol_trade_signature`), could not run; they
  are gone. There is no alias: import the helpers from `dashboard_payloads`
  and the zone builder from `dashboard_zones`. No behaviour changes: 2,000
  fuzzed level-zone builds, 2,000 S/R rows, 1,000 exit records, 200
  snapshots and charts on the recorded tapes and 24 published engine states
  (12 real bots, two cycles each) read byte for byte as before. Tests:
  `tests/test_dashboard_zones.py`, `tests/test_dashboard_state.py` (the
  engine's hand-over with the cycle's own warmup summary, and the build's
  S/R rows and shown symbols in order, its 12-row cap, the positions' S/R
  fields, the candidate limit and the exchanges) and
  `tests/test_log_setup.py` (`TestComponentFailureLog`) are new; the helper
  tests in test_silent_excepts, test_fix_dashboard_charting and
  test_bug_regressions import from `dashboard_payloads`, and
  `tests/test_module_layering.py`'s runtime layer gains the two modules.

- **The session archive has its own module, `session_archive.py` (refactor
  cut C39).** *2026-09-27* — `export_session_archive` moved out of
  `session_report.py` with everything only it uses: the config snapshot and
  its secret redaction, the events and decisions read from the day's log, the
  archive bar loader and forward-move helpers, the gate attribution and the
  regime-call outcomes. `session_report.py` keeps the end-of-session report
  and the persistent trades.csv; the archive writes its own trades.csv with
  the report's `TRADE_CSV_COLUMNS` and `trade_csv_row` (the leading
  underscore is gone), an import in one direction only. The exporter is a
  sequence of stage functions (symbols, bars folders, log copy, trades,
  config and account snapshots, events, decisions, manifest), each writing
  one part and reporting its own failure as before; a symbol whose frame
  cannot be read is still skipped per folder, and the stored HTF frame is
  still read at the strategy's `htf_minutes()`, after the resampled
  folders. The `yaml` import guard is gone: the package cannot import
  without PyYAML. There is no alias: import `export_session_archive` from
  `intraday_tv_schwab_bot.session_archive`. The archive's log lines now log
  under `intraday_tv_schwab_bot.session_archive`, so a log filter on the old
  name must change. No behaviour changes: on seven archive scenarios (a
  failing and an empty symbol, a failing HTF read, partial exits, a
  prior-day trade, an option position, a real config and strategy, a log
  with events and decisions; no account, no log, no feed, an unreadable
  trade, a config YAML cannot represent, a strategy without params) the two
  modules write the same 95 files byte for byte and the same log messages.
  Tests: `tests/test_session_archive.py` (new) pins the stages no test
  reached: the archived symbols, the redacted config snapshot, the account
  snapshot, the events and decisions read from the log copy, a failed
  trades export and a failed stage. The archive tests import from
  `session_archive`, and `tests/test_module_layering.py`'s runtime layer
  gains it.

- **In-position stop and target management has one home, `TradeManager` in
  `trade_management.py` (refactor cut C38).** *2026-09-27* —
  `RiskManager.update_position` (the stop and target exits with their
  broker-bracket and touch-hold deferrals, the peak give-back floor, the
  option premium ratchet and the adaptive breakeven / profit lock / runner
  extension / trail) and `PositionManager`'s two managers moved there:
  `_sr_flip_management_confirmed` is `TradeManager.manage_sr_flip` and
  `_adaptive_ladder_management` is `manage_adaptive_ladder`, with the touch
  hold's helpers and constants (`_TouchHold`, `_next_unpassed_rung`,
  `_delivered_touch_bar`, `LADDER_TOUCH_CLOSE_POSITION_MIN` and the rest).
  `default_levels` and `trail_allowed` (cut C35) moved from `risk.py` with
  them: the entry gatekeeper and the startup reconciler import
  `default_levels` from `trade_management`, and
  `RiskManager.stock_position_trail_pct` reads `trail_allowed` there.
  `PositionManager.__init__` builds `self.trade_manager` over its own
  config, feed, strategy and audit (the ladder reads its touch-hold switch
  from `strategy.exit_policy`, and the S/R reads use the strategy's
  `htf_minutes()` / `htf_lookback_days()`), and `manage_positions` runs the
  two managers and then `trade_manager.update_position` where it ran them
  before, with the same per-position isolation: a manager that raises is
  skipped, a touch hold a failed ladder pass left is dropped, and the risk
  check still runs. The working-slice check runs the same
  `update_position`. `RiskManager` keeps the entry side (the gates, sizing,
  the daily loss, the cooldowns and the same-level block) and no longer
  imports `broker_payloads`. There is no alias: `RiskManager.update_position`,
  the two `PositionManager` managers, `risk.default_levels` /
  `risk.trail_allowed` and `position_manager._next_unpassed_rung` & co. are
  gone. One log line changes logger: a malformed
  `peak_giveback_min_r_override` warning now logs under
  `intraday_tv_schwab_bot.engine` with the ladder's lines (was
  `intraday_tv_schwab_bot.risk`). No behaviour changes: on 1,200 seeded
  position walks (30,476 management passes over four presets and every
  trade management mode) the moved code returns the same decisions and
  leaves the same levels, metadata, audit records and log messages as
  before. Tests: `tests/test_trade_management.py` (new: the level-check
  tests from `tests/test_risk_manager.py`, C35's `TestDefaultLevels` and
  `TestTrailAllowed`, a 3R+ give-back tier case they did not pin, new
  sr_flip tests (the stop flip and next target, the momentum gate, never
  loosening, and the mode / option / frame / S/R-disabled gates) and the
  per-position call order),
  `tests/support/brokers.py` (`_trade_manager`, `_holding_trade_manager`),
  `tests/test_module_layering.py` (the runtime layer gains
  `trade_management`) and the call sites in test_ladder_touch_hold,
  test_bug_regressions, test_properties, test_bracket_orders,
  test_position_isolation, test_partial_exit, test_sweep_fixes,
  test_startup_reconciler, test_exit_levels_logging, test_quote_price_reads,
  test_side_tolerance and test_strategy_requests.

- **An option signal is known by its asset type, and the entry stage
  requires one (refactor cut B18).** *2026-09-27* —
  `SharedEntryPolicy.emit` raises `ValueError` for an option strategy's
  signal whose `metadata['asset_type']` is not `OPTION_VERTICAL` or
  `OPTION_SINGLE`, like its other contract checks (a present `EQUITY` or
  blank stamp is refused too). The risk manager's option checks on a
  signal (the one-slot-per-underlying position cap, the `allow_short`
  exemption, the same-level block's market direction and level, the
  fib-pullback override) read that asset type (`models.is_option_asset`)
  instead of the strategy name, so no runtime module but the screener
  client imports the plugin catalogue, and `tests/test_module_layering.py`
  holds it there. `RiskManager.market_side` and `same_level_anchor` lose
  their `strategy` argument: `market_side(side, metadata)`,
  `same_level_anchor(side, metadata, price)`. The ENTRY_CONTEXT,
  TRADE_SUMMARY and EXIT_CONTEXT `asset_type` fields still log the stored
  value, null for an equity. The plugin guide's `emit` contract
  (`_strategies/README.md`) and the scaffold's option template name the
  rule.

  **Behaviour change:** none for the shipped strategies (both 0DTE
  strategies stamp every signal, and neither builds divergence-only
  entries). An option signal without the stamp now fails the strategy's
  entry cycle; before, risk treated it as an option by name while the
  gatekeeper's order path, which reads the asset type, took it for an
  equity entry on the underlying (checked against the ETF's price, and,
  for a LONG with no target, sent as an equity order). Tests:
  `tests/test_shared_entry_policy.py`, `tests/test_risk_manager.py`,
  `tests/test_option_same_level_block.py`,
  `tests/test_report_skip_buckets.py`, `tests/test_module_layering.py`.

- **The active strategy's option-ness is `BotConfig.active_is_option`
  (refactor cut C37).** *2026-09-27* — the cycle gate (an option strategy
  enters in the regular session only), the startup reconciler (an option
  strategy's broker positions are not restored, and block new entries) and
  the engine's startup log read `config.active_is_option`, the active
  strategy's manifest `plugin_type`, instead of importing the plugin
  catalogue's `is_option_strategy` / `option_strategy_names`. It is a
  property of the strategy name, not a flag set at load, so it cannot go
  stale when `config.strategy` is reassigned (tests and helpers do). The
  load-time check that an option strategy names `options.underlyings`
  keeps `is_option_strategy`: it runs before the `BotConfig` exists. No
  behaviour changes. Tests: `tests/test_asset_type.py` (every shipped
  preset against its manifest; a reassigned strategy),
  `tests/test_cycle_gate.py` (`TestEntriesActionable`),
  `tests/test_startup_reconciler.py` (an option strategy's broker
  positions block entries).

- **One asset-type read, and position math in `position_metrics` (refactor
  cut C36).** *2026-09-27* — `models.asset_type_of(metadata)` (upper-cased,
  `EQUITY` when none is named) and `models.is_option_asset(metadata)`
  replace about twenty hand-rolled reads across the entry gatekeeper,
  execution, the paper account, the position and risk managers, the
  startup reconciler, the dashboard and both 0DTE strategies, the
  gatekeeper's literal `{'OPTION_VERTICAL', 'OPTION_SINGLE'}`, the
  dashboard's `startswith("OPTION")` and `BaseStrategy._is_option_position`.
  The risk manager's stock-notional total and the position manager's exit
  registration read the position's own asset type instead of asking the
  plugin catalogue by strategy name. The three log passthroughs (the
  ENTRY_CONTEXT signal snapshot, TRADE_SUMMARY and EXIT_CONTEXT) keep the
  stored value, null for an equity. `BaseStrategy._position_r_multiple`,
  `_underlying_entry_price` and `_underlying_extremes` are now
  `position_metrics.position_r_multiple`, `underlying_entry_price` and
  `underlying_extremes` (the exit policy and microcap_pm_breakout call
  them). `position_metrics.favorable_move` and `return_pct` are the one
  per-unit move and return-% formula, used by the paper account (its
  unrealized P&L is `position_metrics.position_unrealized_at_price`;
  `PaperAccount._position_unrealized` is gone), the position manager's
  excursion tracking and exit context, and the risk manager's R math (the
  trade manager's since cut C38); each caller keeps its own answer for a
  zero entry (the paper account 0.0, `position_return_pct_at_price` None).
  There is no alias. No behaviour changes: the numbers are bit for bit the
  same but for the sign of one zero, and every builder stamps one of the
  upper-case option types. The zero: EXIT_CONTEXT's `stop_r` / `peak_r` of a
  SHORT whose stop or peak sits exactly at the entry (a stop moved to
  breakeven, a trade that never went its way) log `0.0`, where they logged
  `-0.0`; the numbers are equal, only the log text differs. On inputs
  nothing produces, an asset type now reads the same in lower case, and the
  dashboard no longer draws a bare `OPTION` or an unknown `OPTION_*` type as
  an option. Tests: `tests/test_asset_type.py` (new; it also pins the
  asset-type branches the cut rewrote that no test reached: an unsettled
  option entry's adoption, the per-contract price a working option exit's
  fill is booked at, an option's management price when the strategy has no
  mark, the option entry retry backoff and the entry record's option risk
  fields), `tests/test_properties.py` (a5, a6),
  `tests/test_dashboard_cache.py` (`TestPositionMarkers`),
  `tests/test_execution_invariants.py` (`TestTheCloseSessionGate`, and the
  paper account's unrealized P&L by side).

- **One default-level rule and one trail rule, with the trail pct on
  `RiskManager` (refactor cut C35).** *2026-09-27* —
  `risk.default_levels(side, entry_price, risk_cfg)` replaces
  `EntryGatekeeper._fallback_equity_levels`, the gatekeeper's inline
  post-fill copy and the startup reconciler's restore copy;
  `risk.trail_allowed(mode, metadata, *, options)` replaces the
  gatekeeper's and `RiskManager.update_position`'s copies of the "does this
  position trail" rule. `stock_position_trail_pct` moved from
  `EntryGatekeeper` to `RiskManager`, so `StartupReconciler` no longer takes
  it as an injected callable (the `stock_position_trail_pct=` constructor
  argument is gone; it asks its `risk`). Both functions sit in `risk.py`
  until `trade_management.py` exists (cut C38). There is no alias. The
  entry path, `update_position` and the trail pct read as before. One
  reading changed, on inputs no shipped preset produces: a position
  restored without saved levels gets the entry path's floors, a LONG stop
  and a SHORT target of at least $0.01; they could be $0.00 at
  `default_stop_pct` / `default_target_pct` 1.0, or under a cent on a
  broker average price under about 1.04 cents. Tests:
  `tests/test_risk_manager.py` (`TestDefaultLevels`, `TestTrailAllowed`; in
  `tests/test_trade_management.py` since cut C38),
  `tests/test_startup_reconciler.py` (`TestLevelRestore`: the floors, and
  the trail of a basic and of a saved-row restore through the real risk
  manager).

- **One JSON normalizer, `audit_logger.json_safe` (refactor cut B17).**
  *2026-09-27* — `json_safe(value, *, non_finite)` replaces
  `audit_logger._json_ready` (the TRADEFLOW records, and the stored
  position metadata and risk state: `non_finite="keep"`, NaN written as
  `NaN`) and `dashboard._json_safe` (the dashboard state and chart
  payloads: `non_finite="null"`). It lives in `audit_logger`, not the
  foundation's `serialization`: its last fallback reports through
  `log_setup.warn_once`, an edge the foundation layer does not have.

  **Behaviour change (the written form only):**
  - A numpy integer is written as an int and a numpy bool as a bool
    everywhere: ENTRY_CONTEXT, EXIT_CONTEXT and the stored metadata read
    `3` and `true` where they read `3.0` and `1.0`.
  - A date or datetime (a pandas Timestamp, a numpy datetime64) is written
    with `isoformat()` everywhere: the dashboard's has a `T` between the
    date and the time where it had a space. The page's `Date.parse` and
    `replace('T', ' ')` displays read both forms. A numpy datetime64 is read
    as a pandas Timestamp at any unit: at the ns unit pandas uses it was
    written as its nanoseconds (an int on the dashboard, a float in the
    audit records), and a day-, month- or year-unit one reads as midnight on
    its first day (`2026-09-17T00:00:00`, `2026-09-01T00:00:00`) where the
    audit side wrote the unit itself (`2026-09-17`, `2026-09`, `2026`) and
    the dashboard a date (`2026-09-17`, `2026-09-01`). Its NaT is null on
    both sides (the audit side wrote `NaT`); a pandas NaT still reads `NaT`.
  - A duration is written the same way on both sides now: a pandas
    Timedelta reads `0 days 00:05:00` (the audit side wrote `P0DT0H5M0S`),
    and a numpy timedelta64 what `.item()` gives: a timedelta's `0:05:00`
    (the audit side wrote `5 minutes`), an int count at the ns, month and
    year units (the audit side a float) and null for its NaT (the audit
    side `NaT`). A plain timedelta reads `0:05:00` as before. None is
    written as an ISO 8601 duration: a timedelta has no `isoformat()`, and
    a timedelta64 in months or years has no pandas Timedelta.
  - On the dashboard a Decimal or Fraction is written as a number, not its
    string (a NaN one as null), and a one-element array or Series as its
    string, not its element. A value whose `__str__` raises is written as
    `<unserializable TYPE>` there too, where it failed that update.
  - None of these reach either writer today. Over the whole test suite no
    signal, record or stored metadata carried a numpy int or bool, and
    every time the dashboard shows is written with `isoformat()` where it
    is built.

  The dashboard's `.item()` guess and the audit side's `isoformat()` guess,
  each behind a broad `except` logged at DEBUG, are gone: numpy scalars are
  recognized by type, and only dates and times are asked for
  `isoformat()`. An unknown `non_finite` raises `ValueError`. There is no
  alias. Tests: `tests/test_audit_logger.py` (`TestJsonSafe`, both modes,
  with the dashboard's tests moved in; a datetime64 at every unit and the
  durations), `tests/test_dashboard.py` (`TestTheServedJson`: a chart's NaN
  and infinities are served as null), `tests/test_position_store.py`,
  `tests/test_risk_state_persistence.py` (the risk tallies keep NaN).

- **The reconcile metadata store skips its own unchanged saves; the entry
  and exit metadata filter and the dashboard cache prune have one home each
  (refactor cut C34).** *2026-09-27* —
  - `ReconcileMetadataStore.save_if_changed(positions, *, replace_all)`
    replaces the engine's `_reconcile_metadata_signature`, a hand-kept copy
    of the store's row serialization. The store compares the rows it would
    write, `updated_at` aside, with its last write and skips a save that
    changes nothing. It never skips a replace that follows an upsert, which
    the engine's signature reset after the first successful reconcile was
    for. `IntradayBot._save_reconcile_metadata` is one call: an upsert until
    a broker reconcile has succeeded, then a replace, as before. A failed
    save still logs `Could not save startup reconcile metadata`, now under
    `intraday_tv_schwab_bot.position_store`. A successful reconcile after
    the first in a process no longer rewrites unchanged rows, so their
    `updated_at`, which nothing reads, keeps its time.
  - `structured_metadata_snapshot`, the filter of the position metadata
    ENTRY_CONTEXT and EXIT_CONTEXT carry, is a function in
    `audit_logger.py`. `EntryGatekeeper.structured_metadata_snapshot` and
    `PositionManager`'s `structured_metadata_snapshot` argument are gone.
  - The dashboard update drops the cached snapshots and charts of symbols
    it no longer shows through `DashboardCache.prune_inactive_symbols`
    instead of an inline copy of it.
  - No behaviour changes. There is no alias: import
    `structured_metadata_snapshot` from `intraday_tv_schwab_bot.audit_logger`.
    Tests: `tests/test_position_store.py` (`TestSaveIfChanged`),
    `tests/test_dashboard_cache.py` (new),
    `tests/test_dashboard_update_failure.py` and
    `tests/test_exit_levels_logging.py` (ENTRY_CONTEXT and EXIT_CONTEXT
    leave out an order spec, an option leg and a key on no list).

- **EXIT_CONTEXT names the family that decided the exit (refactor cut
  B15).** *2026-09-27* — `exit_reason_family` is the `family` of the
  `ExitDecision` the exit was taken for, and
  `position_metrics.exit_reason_details` takes the decision. The family was
  guessed from the reason string (`risk` / `schedule` / `technical` /
  `strategy`) and logged beside the decision's own `exit_family`, which is
  gone: EXIT_CONTEXT carries one family key. A bracket child the broker
  filled (`broker_stop` / `broker_target`, found by the fill reconcile,
  before a cancel landed, or at the startup settle) is a `risk` exit. A
  working exit order's fill keeps the family it was sent for, read from the
  order's record (its only writer stores the reason and the family, so the
  fill reads them without a fallback, before it marks the fill booked: a
  record without them raises on every cycle with the fill still unbooked,
  where marking it first would have lost the fill). `exit_reason_code` was
  already `reasons.exit_reason_code` (cut C21). `RISK_EXIT_CODES` is gone;
  the guess was its only reader. The touch-hold entry under Added now names
  `exit_reason_family` alone.

  **Behaviour change (logs only; no decision, order, report bucket or
  dashboard reads the family):**
  - peak-giveback exits read `risk` (was `strategy`);
  - force flatten reads `force_flatten` (was `schedule`);
  - these read their own family (all were `strategy`): the time stop
    (`time_stop`), the chart, candle and structure exits (`chart_pattern`,
    `candle_pattern`, `structure_choch`, `structure_bias`), the S/R-loss
    exit (`sr_loss`) and the divergence scale-out (`divergence_partial`);
  - filled bracket children read `risk` (were `strategy`).

  The stop, the target, the touch hold's three exits, the technical exits
  and a strategy's own exits read as before. Comparing archived
  EXIT_CONTEXT rows across this date means mapping the old values, or
  reading the old rows' `exit_family` where the manager wrote one. Tests:
  `tests/test_reasons.py` (keeps the old guess as an oracle),
  `tests/test_partial_exit.py` (`TestTheExitFamily`, every exit path and a
  record without its decision), `tests/test_ladder_touch_hold.py`.

- **The strategy owns its exit policy, and an exit decision sizes itself
  (refactor cut C33).** *2026-09-27* — `BaseStrategy.__init__` builds
  `self.exit_policy = SharedExitPolicy(config, self)` beside
  `self.entry_policy`, and the position manager decides each open
  position's exit through `strategy.exit_policy`; the adaptive ladder's
  touch hold reads its timeout there too. The manager no longer builds its
  own copy or imports `_strategies.shared_exit`.
  `shared_exit.partial_exit_qty(qty, fraction)` is now
  `ExitDecision.close_qty(qty)` in `models.py`: the whole quantity at a
  fraction of 1.0, otherwise the floor of the product rounded to 9 places.
  There is no alias: `partial_exit_qty` and `PositionManager.exit_policy`
  are gone. No behaviour changes: the engine builds the strategy and the
  position manager from one config, which the policy reads live. Tests:
  the sizing cases moved from `tests/test_shared_exit_policy.py` to
  `tests/test_partial_exit.py` (`TestCloseQty`); stubs that set
  `manager.exit_policy` now set `manager.strategy.exit_policy`;
  `TestTheStrategyOwnsThePolicy` pins the ownership.

- **The dashboard draws the order blocks and fair value gaps the strategy
  read (refactor cut B14).** *2026-09-27* — the LTF FVG and the HTF / LTF
  order-block overlays ask for the strategy's `ltf_fvg_request()` /
  `order_block_request()` (public; `_order_block_tuning_knobs` is gone, and
  the request's keys are now the feed's parameter names, `min_block_atr_mult`
  / `min_block_pct`) at the strategy's price, the close of the 1m frame's
  last bar, and the HTF context, the level zones and the sidebar's trend
  read take `htf_fvg_request()`; the dashboard resolved every knob from the
  config itself (the same values). The strategy's own LTF FVG and order
  block reads pass the requests through as they are.

  **Behaviour change:** the overlays were built at the quote's last
  whenever the quote was fresh, a price the strategy never judged, and
  missed the data feed's cycle cache; they now hit it. On the 11 presets
  that draw LTF gaps, over 27,349 archived top_tier checkpoints (8 days),
  the drawn set differs at 0.02% (a quote just after the bar) to 0.17% (a
  minute later) of them: a gap entering or leaving the top 3 by distance,
  or one at the size floor. No preset draws order blocks. The `or default`
  reads (a configured 0 read as the default) are unchanged on both sides.
  Tests: `tests/test_strategy_requests.py`
  (`TestTheDashboardDrawsTheStrategysContexts`, the request keys and the
  knob each key reads), `tests/test_sr_snapshot.py` (the sidebar trend's
  HTF request, since cut C41), `tests/test_config_validation.py`.

- **The score context is the FVG term's context (refactor cut B13).**
  *2026-09-27* — `BaseStrategy._default_htf_request()` is the HTF request
  the score context (`_default_htf_context_for_score`), the shared FVG score
  term and zero_dte's entry read and prefetch share; the score context goes
  through `_htf_context`, so it carries the strategy's FVG arguments
  (`htf_fvg_request()`, public now; was `_htf_fvg_request`). zero_dte's
  `_htf_fvg_context_request` and the FVG term's copy of the request are
  gone. The peer family's ladder exit and HTF request read
  `htf_minutes()` / `htf_lookback_days()` instead of the params with a
  literal 60 (every peer manifest declares 60/60, so they read the same).

  **Behaviour change:** none in any score, and one HTF build fewer per
  symbol per stored-frame change. The FVG arguments are part of the data
  feed's cache key and every preset sets them off the feed's defaults
  (3 / 0.06 / 0.0006), so the score context was a second build of the same
  frame on every preset. FVG detection reads nothing else, and no score
  reads the score context's FVGs: on 463 checkpoints x 2 presets
  (top_tier_adaptive, peer_confirmed_key_levels) on the fixture and archive
  15m frames, every other field is identical. With no feed or no stored
  HTF frame the score context is the empty context instead of `None`,
  which every reader takes the same way (no EMAs, a neutral trend, no
  divergence). Tests: `tests/test_strategy_requests.py`
  (`TestTheRequestsNameTheFeedsArguments`,
  `TestTheScoreContextIsTheFvgTermsContext`), `tests/test_htf_ema_knobs.py`,
  `tests/test_fail_closed_gates.py`, `tests/test_sr_tolerance_reads.py`.

- **The strategy's timeframes have one home (refactor cut C32).**
  *2026-09-27* — `BaseStrategy.htf_minutes()`, `htf_lookback_days()` and
  `ltf_minutes()` are public, and the engine (the context refresh and the
  S/R precompute), the position manager (S/R-flip management and the
  ladder's touch hold, the trade manager's since cut C38), the entry
  gatekeeper, the dashboard and the session archive's HTF folder ask the
  strategy for them. `DashboardCache._active_htf_minutes` /
  `_active_htf_lookback_days` / `_active_ltf_minutes`,
  `PositionManager.active_htf_minutes` / `active_htf_lookback_days` and the
  archive's own HTF resolution are gone, and so is the dashboard structure
  overlay's `params.ltf_minutes` read; 0DTE's HTF trend reads the accessors
  instead of the params with a literal fallback (its manifests declare the
  keys). There is no alias. No behaviour changes: every copy was the same
  read. A strategy handed to the dashboard, the position manager or the
  archive needs the methods; tests use
  `tests.support.brokers._StrategyStub`, which borrows BaseStrategy's own.
  The layering guard (`tests/test_module_layering.py`) fails any runtime or
  composition module that reads a timeframe from a strategy's params, so a
  module that joins the runtime layer is covered too. Its one allowlisted
  read is the session archive's resample list, which still reads
  `params.ltf_minutes` / `htf_minutes`: a strategy that declares neither
  gets no resampled folder but `bars/1m`, and asking the strategy would add
  folders at its defaults, a change not decided yet. Tests:
  `tests/test_strategy_requests.py` (new: each reader asks a strategy whose
  own answers differ from its params, from the engine's context refresh and
  S/R precompute, the trade manager's S/R flip and touch hold, the entry
  gatekeeper and the dashboard's rows, charts and structure overlay to
  0DTE's HTF trend and the archive's HTF folder).

- **The quote-price reads name their key order (refactor cut C31).**
  *2026-09-27* — `data_feed` holds the three orders beside the quote
  cache, each read with `numeric.first_float(..., positive=True)`:
  `MANAGEMENT_PRICE_KEYS` (`mark`, `last`, `close`: the manager's
  stop/target price and the restore's current price),
  `EXECUTION_LAST_KEYS` (`last`, `mark`, `close`: the executor's last
  price, and the last price of the manager's market snapshot) and
  `DISPLAY_PRICE_KEYS` (`last`, `mark`, `mid`, `close`, `bid`, `ask`: the
  dashboard). The camelCase keys the manager, the restore and the executor
  also tried (`markPrice`, `lastPrice`, `closePrice`, `bidPrice`,
  `askPrice`) are gone: a cached quote holds `_normalize_quote`'s keys
  only, and a market snapshot bid, ask and last. So are the ones
  `options_mode.contract_from_quote` tried (`putCall`, `strikePrice`,
  `openInterest`, `totalVolume`, `volume`, `daysToExpiration`,
  `inTheMoney`): every caller hands it a cached quote and, as its fallback,
  a stored leg (`asdict(OptionContract)`, since the first commit), so none
  was in either; a raw chain entry is `parse_option_chain`'s to read. For
  the same reason it reads the greeks, open interest, days to expiration
  and moneyness from the stored leg alone; the quote, which holds none of
  them, is read first only for the prices and the volume. An
  option position's underlying price, when no bar has one, is its quote's
  `mark` read the same way (an int too large for a float no longer raises
  out of the cycle). One reading changed: the snapshot's last price was
  `safe_float(last or mark or close)`, so a NaN or unparseable `last` read
  as None and a negative price as itself; it now falls through to the next
  positive key. The cache never holds a NaN or unparseable price, and holds
  a negative one only when the broker sends one; every other cached quote
  reads as before. Tests: `tests/test_quote_price_reads.py`,
  `tests/test_numeric.py`, `tests/test_options_nan_fallbacks.py`
  (`TestContractFromQuote`), `tests/test_properties.py` (n12).

- **Broker payload parsing has one home, `broker_payloads.py` (refactor
  cut C30).** *2026-09-27* — `broker_positions.py` is renamed
  `broker_payloads.py` and takes the order readers that were
  `SchwabExecutor` classmethods, as free functions: `order_status`,
  `order_remaining_qty`, `order_filled_qty`, `order_fill_price`,
  `order_is_filled`, `order_is_terminal_failure`,
  `extract_bracket_children`, `flatten_order_tree` and
  `collect_protective_fills` (with the private `_order_executions` and
  `_multi_leg_net_fill_price`). The executor keeps the I/O. The broker's
  vocabulary is named once: `WORKING_STATUSES` (the working-order
  listing's inline set), `TERMINAL_FAILURE_STATUSES` (was
  `SchwabExecutor._EQUITY_TERMINAL_FAILURE_STATUSES`) and
  `STOP_ORDER_TYPES` (was `_BRACKET_STOP_ORDER_TYPES`, and a literal in the
  restore's resting-stop match). A bracket record's order-id keys are
  `BRACKET_ID_KEYS` (`BRACKET_WRAPPER_KEYS` + `BRACKET_CHILD_KEYS`):
  - `bracket_order_ids(bracket)` reads them for the reconcile's
    foreign-order check, the settle's cancelled set and the manager's
    unlisted-child cache;
  - `bracket_wrapper_and_children(bracket)` reads them for
    `cancel_bracket`, where the OCO still wins over a protective order
    beside it;
  - adoption and the dry-run mirror iterate `BRACKET_ID_KEYS` (was
    `SchwabExecutor._PROTECTION_ID_KEYS`).

  Five enumerations of the keys (two in the reconciler, two in the
  executor, one in the manager) had to agree; a key one of them missed
  read an owned order as foreign, which blocks every entry. There is no
  alias: import from `intraday_tv_schwab_bot.broker_payloads`. With
  `_sr_ladder` (cut B9) and `broker_positions` gone, no module imports a
  retired module, and the layering guard's allowlist for those imports is
  empty. No behaviour changes: the unlisted-child cache's prune now keeps
  an entry whose id is still one of the bracket's wrapper ids (the old stop
  of a standalone protective stop after a replace, which moves the stop and
  child ids to the new order and leaves `protective_order_id` at the old
  one), and nothing reads that entry again. Tests:
  `tests/test_broker_payloads.py` (new; the order readers'
  `TestBrokerPayloadParsing` moved there from
  `tests/test_execution_invariants.py`), `tests/test_bracket_orders.py`,
  the former classmethod calls in `tests/test_sweep_fixes.py`,
  `tests/test_runtime_nan_reads.py` and `tests/test_properties.py`, and the
  restore adopting a resting `STOP_LIMIT` as it does a `STOP`
  (`tests/test_sweep_fixes.py`).

- **The support/resistance ladder is collapsed once, and cut after the
  broken-level drop (refactor cut B8).** *2026-09-27* —
  `build_support_resistance_context` collapsed each side's candidates into
  rungs and cut the side to `max_levels_per_side`; then
  `_reconcile_flipped_levels` dropped the rungs near the broken levels and
  collapsed what was left a second time, even when there was no broken
  level. A rung keeps its strongest member's price, not the cluster's mean,
  so two rungs could sit within `side_tolerance` of each other, and the
  second pass merged them and summed their touches and score again: with a
  $1 tolerance, candidates at 99.1, 100.0 (three touches) and 100.9 collapse
  to 100.9 (1 touch) and 100.0 (4), and the second pass made them one
  support at 100.0 with 5. And because the cut came first, a rung dropped
  next to a broken level left the side one rung short while deeper
  candidates existed. Now each side is collapsed once, on its whole
  candidate pool (`levels_shared.collapse_same_side_levels(...,
  max_levels=None)` keeps every rung); `_reconcile_flipped_levels` only
  drops the supports within `side_tolerance` of the lost support and the
  resistances within it of the reclaimed resistance, reading each rung's
  price as before; and the side is cut to `max_levels_per_side` after that.
  The HTF level map already collapsed once and is untouched.

  **Behaviour change:** a side's ladder only gains rungs: a rung the second
  pass had merged comes back on its own (the rung that absorbed it drops
  back to its one-pass touches and score), and a side a drop had left short
  refills from the deeper candidates. Every rung the old ladder held is
  still there, at the same price, so `nearest_support` /
  `nearest_resistance` can only move nearer to price. On the 25 archived
  sessions with 15m bars (519 symbol-days, one build every 15 minutes from
  09:45 to 16:00, preset settings), with the 1m/5m flip confirmation the
  entries read, the ladder changes in 11% of builds at 4 levels a side
  (top_tier_adaptive, small_cap_squeeze, the peer and microcap presets) and
  9% at 3 (the others, both 0DTE presets among them); without it (the
  cached and dashboard context) 9% and 7%. The nearest support moves in
  2.0% of builds and the nearest resistance in 0.5%, always nearer, from a
  median 1.5 ATR to 0.9 ATR; `near_support` changes in 0.8%, `bias_score`
  in 1.5% and `regime_hint` in 0.1%. Those feed the S/R veto where
  `use_sr_filter` is on (in 1.2% of builds the nearest level newly sits
  inside `entry_min_clearance_*`: 136 supports, under a SHORT, and 21
  resistances, over a LONG, of 13,467), the S/R stop and target refinement,
  the proximity scoring, the adaptive ladder's rungs (top_tier_adaptive,
  small_cap_squeeze), htf_pivots' level candidates and the dashboard's S/R
  ladder. The broken and pending levels and the market structure do not
  change.

  The three support/resistance snapshots regenerate byte-identical: on those
  builds no two rungs sat within tolerance and no rung sat near a broken
  level. Tests: `tests/test_sr_collapse_once.py` (new). Two tests fail on
  the old code (the 99.1 / 100.0 / 100.9 example; a drop that left the
  ladder short); the third pins that the drop reads the rung's price, not
  its members'. `tests/test_levels_shared_steps.py` pins
  `max_levels=None`.

- **One side-tolerance formula; `_sr_ladder.py` is gone (refactor cut B9).**
  *2026-09-27* — `levels_shared.side_tolerance(atr, price, *,
  atr_tolerance_mult, pct_tolerance, min_gap_atr_mult, min_gap_pct)`, the
  larger of the merge tolerance (the ATR or the price arm) and the same-side
  minimum gap (`same_side_min_gap_threshold`), is the one spacing formula:
  the S/R build collapses its ladders at it and publishes it as
  `side_tolerance`, and the HTF build collapses at it too.
  `levels_shared.effective_side_tolerance(sr_cfg, price, *, atr=0.0,
  sr_ctx=None)` returns the S/R context's tolerance when it is above 0, else
  the formula from `config.support_resistance`; sr_flip management spaces
  its next target with it, the dashboard its S/R ladder.
  `select_next_distinct_level` and `collapse_price_ladder` moved into
  `levels_shared` under public names; the first no longer reads a missing
  gap as 0 (its one caller always passes the spacing). `_sr_ladder.py`, its
  `_same_side_ladder_min_gap_pct` and its engine-named logger are gone, and
  there is no alias: import from `intraday_tv_schwab_bot.levels_shared`.
  **Behaviour change:** the fallback wrote the formula out again with 1e-4
  floors the builders never had (they bound only below $0.0333 at every
  shipped preset's tolerances), read a None price or ATR as 0, and fell back
  to the config behind a broad except when the context's tolerance did not
  read; now there is no floor, the price is required, and such a context
  raises. The dashboard computes its ladder spacing only for a symbol with an
  S/R row, so a symbol without one (S/R off) and without a price yet no
  longer reads the tolerances. The fallback runs only on an empty S/R context,
  which has no levels to space: on the fixture tapes (AAPL / SPY / TSLA,
  1,314 checkpoints every 5 minutes over two sessions) every context carried
  a positive tolerance, and the old and new spacing agreed at every
  checkpoint with and without the ATR. The S/R and HTF snapshots are
  byte-identical. Tests: `tests/test_side_tolerance.py` (was
  `tests/test_sr_ladder.py`, rewritten: the formula, the fallback, that the
  fallback lands on the build's own tolerance, and the two readers: the
  sr_flip target, which no test ran before, and the dashboard's rungs),
  `tests/test_config_validation.py`, `tests/test_module_layering.py` (two
  allowlisted imports gone).

- **Divergence has its own module, `divergence.py` (refactor cut C25).**
  *2026-09-27* — `DivergenceMatch` and `find_divergence` moved out of
  `levels_shared.py`, which keeps the level primitives, the ladder steps
  and the prior day/week levels. The new `divergence_inputs(frame, highs,
  lows, *, in_session=None)` returns `(highs, lows, bar_clock,
  price_scale)`: the session-bar pivots, the session clock and the gap-free
  price scale that `technical_levels` and `htf_levels` each prepared with a
  copy of their own. Each builder now computes its frame's session mask
  once and hands it to every session-aware read as `in_session`:
  `divergence_inputs`, `indicators.session_price_scale`,
  `indicators.latest_atr14` (through `atr_with_floor`), and in the technical
  build the impulse pivots and a non-default divergence RSI. The technical
  build computed the mask up to six times and the HTF build three. The
  builders' own lookback and age clamps are gone; `find_divergence` applies
  the same two (`max(2, ...)`, `max(0, ...)`). The presets' and
  `config.py`'s comment on the divergence knobs names
  `divergence.find_divergence`. There is no alias: import from
  `intraday_tv_schwab_bot.divergence`. `tests/test_levels_shared_divergence.py`
  is now `tests/test_divergence.py`. No behaviour changes: the level
  snapshots are byte-identical.

- **The S/R ladder steps and the swing reduction live in `levels_shared`
  (refactor cut C28).** *2026-09-27* — `collapse_same_side_levels`,
  `drop_levels_near_price`, `partition_levels_by_side`, `pending_level`,
  `split_references_by_flip`, `detect_broken_levels` and the `FlipCheck`
  type replace the private copies in `htf_levels` and `support_resistance`.
  `build_htf_context` no longer inlines its side split, its partition closure
  and its broken-level loops, and both builders' second-chance fallback loop
  side-assigns what it adds through `split_references_by_flip`. The builders'
  deliberate differences are arguments at their call sites:
  - `reduce=`, the cluster reducer: HTF keeps one representative
    (`_representative_level`), SR merges the cluster (`_merge_level_group`);
  - `relabel=`, the flipped levels' source: HTF names them
    `broken_htf_support` / `broken_htf_resistance`, which the peer strategies
    read; SR keeps the source;
  - `gate_tol=`, how far short of the close a confirmed flip still counts as
    broken: float noise (1e-6 of the close) for HTF, the merge tolerance for
    SR.

  Only SR re-drops its ladder near the confirmed flips
  (`_reconcile_flipped_levels`). `reduce_pivots(highs, lows, *,
  min_gap_bars=0)` replaces the alternating-swing loop that market structure
  (with its minimum gap) and the technical levels (without one) each carried
  as `_reduced_pivots`; technical's never-taken pivot recompute and SR's
  redundant empty-frame check go with them. There is no alias; the private
  names are gone. No behaviour changes: the 10 snapshot JSONs are
  byte-identical, and the four builders return identical contexts before and
  after on 12,000 randomized builds, half of them with random flip patterns.
  SR now tests the broken-level price gate before the flip check, as HTF
  did, which saves flip checks and changes no result.
  `tests/test_levels_shared_steps.py` pins each step and the three builder
  choices.

- **One level class, `levels_shared.Level` (refactor cut C27).**
  *2026-09-27* — `htf_levels.HTFLevel` and
  `support_resistance.SupportResistanceLevel` had the same eight fields
  (`kind`, `price`, `touches`, `score`, `first_seen`, `last_seen`, `source`,
  `source_priority`); only the HTF class defaulted `touches` (1) and `score`
  (1.0). `levels_shared.Level`, with those defaults, replaces both, in
  `HTFContext`, `SupportResistanceContext` and every builder step.
  `cluster_levels`, `clone_level`, `frame_extreme_side_levels` and
  `fallback_prior_side_levels` build a `Level` and lose their
  `level_factory` argument. `build_special_level`, whose one caller is
  `fallback_prior_side_levels`, is private now (`_build_special_level`).
  The private wrappers that only bound that argument or `include_idx` are
  gone: `_pivot_points`, `_cluster_levels`, `_clone_level`,
  `_frame_extreme_side_levels` and `_fallback_prior_side_levels` in
  `htf_levels` and `support_resistance`, and `_pivot_points` in
  `technical_levels`; the builders call `levels_shared` directly. In both
  builders a side with no pivots and no prior-day/week level takes the frame
  extreme through `frame_extreme_side_levels` instead of an inline copy of
  it. `build_htf_context` detects pivots once per build, with their bar
  positions, and its RSI divergence reuses them; it ran the detector a
  second time over the same frame and span. There is no alias: import
  `Level` from `intraday_tv_schwab_bot.levels_shared`. The level snapshots
  are byte-identical. No behaviour changes. Tests:
  `tests/test_fix_key_levels.py`
  (`TestASideWithNoPivotsOrPriorLevelsIsTheFrameExtreme`,
  `TestHTFDetectsPivotsOnce`).

- **Fair value gaps have their own module, and the zone arithmetic one home
  (refactor cut C26).** *2026-09-27* — `fair_value_gaps.py` holds
  `HTFFairValueGap`, `FairValueGapContext`, `empty_fvg_context`,
  `build_fair_value_gap_context` and the detector, public now as
  `detect_fair_value_gaps` because the HTF context calls it; they moved out
  of `htf_levels.py`, which keeps the HTF levels, trend and divergence.
  `zones.py` holds the arithmetic the gaps share with order blocks:
  `zone_distance` (the distance to a zone, which never depended on its
  direction) replaces `_fvg_distance` and `_ob_distance`;
  `zone_filled_pct(..., bullish=)` replaces the gap detector's two inline
  fill formulas and `order_blocks._filled_pct_for_bullish` / `_bearish`; and
  `zone_sizing` returns the minimum size, comparison margin and merge
  tolerance both builders computed with the same constants. The two merges
  stay separate on purpose: a merged gap keeps the smaller `filled_pct` of
  its parts and a merged order block the larger (`tests/test_zones.py` pins
  both). The completed-bar cut that the HTF flip confirmation and the gap
  detector read is `bars.completed_bars` (was
  `htf_levels._completed_htf_frame`). Strict-mode order blocks take their
  swings from `levels_shared.pivot_points(..., include_idx=True)` instead of
  support_resistance's private wrapper, the gap and order-block builders no
  longer copy the frame before `ensure_ohlcv_frame` (which copies), and
  `technical_levels` and `strategy_base` import at module top what they
  imported inside a function. There is no alias: import from
  `intraday_tv_schwab_bot.fair_value_gaps`, `zones` and `bars`. No
  behaviour changes; the level snapshots are byte-identical.

- **Candle detection reads its bars once and keeps the longest tier once
  (refactor cut C29).** *2026-09-27* — `candles._ohlc_subset(frame,
  lookback, *, min_bars)` is the one "last N bars of open/high/low/close,
  read as numbers, less every bar that does not read" step. The
  latest-snapshot key (`_ohlc_frame_key`, at least 1 bar) and
  `detect_per_bar_candle_patterns` (at least `CANDLE_CONTEXT_BARS`) both call
  it and keep their own floor. `_tier_cascade` is the one longest-tier-wins
  rule; the per-bar map's nested `_apply_tier_cascade` copy is gone. The
  frame key holds plain floats. C15's `numeric.safe_float` there only ever
  saw numbers that `pd.to_numeric` + `dropna` had already cleaned, so the
  key's `float | None` type, the tweezer helpers' None guards and the per-bar
  map's unreachable empty-key and length checks go, and candles no longer
  imports `numeric`. No output changes: the candle context, the opt-in
  `detect_bullish_patterns` / `detect_bearish_patterns` and the per-bar map
  read the same on every input tried (NaN and unparseable cells, infinities,
  Int64 / Float64 columns, naive and non-timestamp indexes, lookbacks from -3
  to 500). The one input that reads differently is an OHLC column of complex
  numbers, which used to read as "no pattern" and now raises TypeError; no
  feed produces one. `tests/test_pattern_indicator_regressions.py` pins both
  floors, the dropped-bar alignment and the cascade.

- **Brackets with a resting target now load with `adaptive_ladder`, unless
  the touch hold is on.** *2026-09-27* — `execution.bracket_legs:
  stop_and_target` was refused for every `adaptive_ladder` config, for the
  target-exit suppression and the final-rung runner (both removed, see
  Removed). With the hold off the ladder's first rung is a plain
  take-profit, which a resting target serves as well; with it on,
  `stop_only` is still required. Brackets are off in every preset. Tests:
  `tests/test_bracket_orders.py`, `tests/test_ladder_touch_hold.py`.

- **Every `shared_exit` switch must be `true` or `false` at load.**
  *2026-09-27* — the section is checked like the others
  (`_validate_shared_exit_config`, with the touch hold's timeout in
  `config._NUMBER_CHECKS["shared_exit"]`). `SharedExitPolicy` reads a switch
  with `bool()`, so a quoted `"false"` turned an exit family on and a blank
  one off (`shared_exit.use_structure_exit must be true or false, got
  'false'`). The section's other numbers are the exit policy's and are not
  checked. Every shipped preset loads unchanged. Tests:
  `tests/test_config_validation.py`, `tests/test_ladder_touch_hold.py`.

- **Docs: the peak-giveback tiers, its off switch and what manages a
  small_cap runner.** *2026-09-27* — no behaviour change (the S1 exit
  study).
  - `RiskConfig`'s comment, fifteen presets' `peak_giveback_*` comments and
    top_tier's README table described the tiers before 2026-05-27 ("50% at
    1R-2R, 40% at 2R-3R, 30% at 3R+"). The floor keeps
    `peak_giveback_retain_*` of the peak: 65% / 72% / 78% by default and in
    top_tier, 60% / 70% / 78% in small_cap_squeeze. The README table's
    low-tier fraction (0.45 in top_tier) and override (2.5R at a 2.5% day
    strength) are corrected too.
  - The off switch is `peak_giveback_enabled: false` (the low tier with
    it); `peak_giveback_min_r` must be above 0 and is refused at load
    otherwise. The docs say so.
  - `config.small_cap_squeeze.yaml` and the small_cap README no longer say
    peak giveback owns the runner: the high-conviction override (a 5% day
    strength) was on 37 of the 43 replayed entries, and on those giveback
    arms only at a 2.5R peak, with no low tier. Below that, break-even at
    1.2R, the profit lock at 1.8R (to +1.0R) and, for an entry with no
    ladder rung, the 5% trail (a median 0.32R wide) manage it.
  - `configs/README_PRESETS.md` lists both.

- **`--version`, and the version in the start-up log.** *2026-09-26* —
  nothing reported `intraday_tv_schwab_bot.__version__`. `python main.py
  --version` and `intraday-tv-schwab-bot --version` print
  `intraday-tv-schwab-bot <version>` and exit, and the bot logs the same
  line at start-up, right after `Logging to ...` and before the Schwab
  client is built, so the day's log names the code that wrote it even when
  start-up fails. Tests: `tests/test_packaging.py`, `tests/test_smoke_bot.py`.

- **`pytest-xdist` is declared in the `dev` extra.** *2026-09-26* — the
  suite's parallel run (`pytest -n 24 --dist loadfile`) needs it, and it was
  installed in the working venv but declared nowhere. It is pinned at 3.8.0
  like the rest of the extra; `constraints.txt` still leaves dev-only
  tooling out.

- **A blank or malformed time, number, switch or mode stops the bot at
  startup, naming the key.** *2026-09-26* — configured times were parsed where
  they were read, and a bad one raised parse_hhmm's bare error there ("not
  enough values to unpack" for a value with no colon): a bad
  `afternoon_start_time` at 13:00, a bad `credit_start_time` in the first 0DTE
  entry cycle, a bad blackout time on the event's own date (in the 0DTE
  force-flatten check, the exit path, too), and a malformed
  `options.force_flatten_time` when the options strategy was built. Now:
  - at load: the four `options` times (`force_flatten_time`,
    `debit_target_time_decay_start` / `_end`, `delta_time_shift_start`,
    whatever the strategy), every `strategies.<name>.entry_windows` /
    `management_windows` / `screener_windows` override (these were parsed
    only in the engine's first cycle, where the bot died), and each inline
    `events.blackouts` row's `start` / `end`
    (`options.force_flatten_time must be an HH:MM time, got ''`,
    `events.blackouts[1].end must be an HH:MM time, got '8h30'`);
  - when the strategy is built: its HH:MM params, listed in the new
    `BaseStrategy.time_params` class attribute (top_tier_adaptive /
    small_cap_squeeze: `orb_end_time`, `midday_start_time`,
    `midday_end_time`, `afternoon_start_time`, `no_new_entries_after`,
    `early_session_stop_widening_until`; both 0DTE strategies' window and
    cutoff times; microcap_pm_breakout's `pm_reference_window_start` /
    `_end`), and the `blackout_file` rows. The calendar now reads the file
    whenever a strategy is built; until now only the strategies that consult
    it (top_tier_adaptive, small_cap_squeeze and the two 0DTE strategies)
    read it, at their first blackout check and only with `events.enabled`
    on.

  An unquoted time (YAML's sexagesimal integer) is accepted by every check.
  `parse_hhmm` names the value it cannot read (`Invalid HH:MM time: ''`) and
  raises only `ValueError` (an infinity, or a field past a C int such as
  `2147483648:00`, raised `OverflowError`); the new `sessions.is_hhmm` asks
  the same question without raising.
  **Behaviour change:** a blackout row without `start` or `end`, which was
  skipped without a word, now refuses startup. The checks run whether or not
  the feature reading the time is switched on: a window override in an
  inactive strategy's section is checked, and so is the `blackout_file` of a
  strategy that never consults the calendar or of a disabled `events`
  section, whose "Event calendar file not found" warning now also comes at
  startup. A `blackout_file` edited while the bot runs is checked when it is
  re-read (see the next entry). No shipped preset or feed is affected. Tests:
  `tests/test_sessions.py`, `tests/test_config_validation.py`
  (`TestOptionsValidation`, `TestStrategyWindowValidation`,
  `TestEventsValidation`), `tests/test_strategy_time_params.py` (with a drift
  check that every HH:MM param a manifest or preset ships is listed),
  `tests/test_top_tier_megacap.py` (`TestEventBlackouts`) and
  `tests/test_orb_regime.py`.

  The numbers and switches the engine, the data feed, the risk and position
  managers, the execution layer and the dashboard read are checked at load
  too (`config._NUMBER_CHECKS`, and every field a section's dataclass
  declares `bool`). Most were read where they were used, with `float()`,
  `int()`, `bool()` or as they stood. A typo raised mid-session: in the
  entry or management cycle, after a fill while the filled position was
  being booked (`risk.risk_overage_warn_frac`, `entry_slippage_warn_pct`,
  the `options` premium levels), or between cycles, outside their error
  handling, where it ended `run()` without its shutdown
  (`runtime.loop_sleep_seconds`, `idle_sleep_seconds`,
  `symbol_state_prune_seconds`). A NaN or an infinity passed the clamps and
  comparisons: an infinite `execution.entry_live_fill_timeout_seconds`
  polled an unfilled order forever, an infinite `runtime.quote_cache_seconds`
  left cached quotes unrefreshed, and a NaN `bracket_stop_limit_offset_r`
  priced the resting stop-limit at `nan`. A quoted `"false"` read as on and
  a blank as off, so a blank `schwab.dry_run:` traded live. Now a number
  must be a YAML number (not quoted, not a boolean), finite and inside its
  range, a count an integer, and a switch `true` or `false`
  (`runtime.loop_sleep_seconds must be a finite number > 0, got '2s'`):
  - `schwab`: `timeout` (an integer >= 1), `dry_run`,
    `open_browser_for_auth`;
  - `tradingview`: `max_candidates` (an integer >= 1),
    `screener_refresh_seconds`, `min_market_cap`, `max_market_cap` and
    `min_value_traded_1m` (>= 0), `min_volume` and `min_volume_1m`
    (integers >= 0); 0 turns a 1m filter off;
  - `risk`: `max_positions` (an integer >= 1),
    `risk_per_trade_frac_of_notional`, `default_stop_pct` and
    `default_target_pct` (in (0, 1]), `max_notional_per_trade`,
    `max_total_notional` and `max_daily_loss` (> 0), `cooldown_minutes`,
    `same_level_block_minutes` and `time_stop_minutes` (integers >= 0),
    `same_level_block_atr_mult`, `entry_slippage_allowance_spread_frac`,
    `entry_slippage_allowance_max_pct`, `time_stop_min_return_pct` and
    `peak_giveback_low_tier_min_r` (>= 0), `peak_giveback_min_r` (> 0),
    `peak_giveback_low_tier_giveback_frac` and the three
    `peak_giveback_retain_*` (in [0, 1]), `risk_overage_warn_frac` and
    `entry_slippage_warn_pct` (finite; below 0, or 0 for the slippage,
    turns the warning off), `trailing_stop_pct` (>= 0, or null; 0 or null
    turns the trail off), and its five switches;
  - `runtime`: `loop_sleep_seconds`, `quote_poll_seconds` and
    `history_poll_seconds` (> 0), `idle_sleep_seconds`,
    `symbol_state_prune_seconds` and `quote_cache_seconds` (>= 0),
    `quote_batch_size`,
    `history_lookback_minutes`, `warmup_minutes`, the four `stream_*`
    seconds and `startup_order_lookback_days` (integers >= 1),
    `prewarm_before_windows_minutes` (an integer >= 0),
    `startup_reconcile_ignore_symbols` (a list of tickers: a string was
    read letter by letter, so the symbol it named was not ignored, and a
    blank entry or one that is not a string is refused, each named with
    its index, a boolean or null one with the hint to quote the ticker (an
    unquoted `ON` was read as `TRUE`, so ON was never ignored), as for an
    event row's symbols (`symbols.ticker_quote_hint`); a null list is
    none) and its six switches, beside the three the entries below check
    (`error_escalation_cycles`, `cycle_precompute_workers`,
    `max_consecutive_quote_failures`), which move into the table unchanged;
  - `paper`: `starting_equity` (> 0), `max_equity_points` and
    `max_trade_history` (integers >= 1);
  - `dashboard`: `port` (1-65535), `refresh_ms` (an integer >= 1), each
    chart profile's `max_bars` (1-480) and every switch, the profiles'
    included;
  - `execution`: `entry_limit_min_buffer`, `entry_limit_max_buffer`,
    `entry_limit_spread_frac`, `entry_live_reprice_step_frac`,
    `bracket_stop_limit_offset_r` and `bracket_replace_min_price_delta`
    (>= 0), `entry_live_fill_timeout_seconds` and `entry_live_poll_seconds`
    (> 0), `entry_live_reprice_attempts` (an integer >= 0) and its four
    switches;
  - `events`: `enabled`, and `earnings_block_sessions_before` / `_after`
    (integers >= 0; a typo raised in the entry cycle of a symbol with an
    earnings date);
  - `options`, what the engine itself reads: `max_loss_per_trade`, the
    premium level knobs (`debit_stop_frac`, `debit_target_mult`,
    `credit_stop_mult`, `credit_target_frac`, `single_stop_frac`,
    `single_target_mult`) and the four `options_breakeven_*` /
    `options_profit_lock_*` multipliers (> 0), `max_quote_age_seconds`,
    `dry_run_step_frac` and `debit_stop_time_decay_widen_factor` (>= 0),
    `max_contracts_per_trade` (an integer >= 1), `dry_run_replace_attempts`
    (an integer >= 0), and every `options` switch;
  - `support_resistance`: the four level-spacing tolerances
    (`atr_tolerance_mult`, `pct_tolerance`, `same_side_min_gap_atr_mult`,
    `same_side_min_gap_pct`; above 0), which the S/R and HTF builds, the
    feed's HTF-frame reads, the strategies' HTF context and market-structure
    reads (the strategy base, the shared entry stage, zero_dte), the
    dashboard and the ladder spacing read (see "The data feed, the S/R
    ladder spacing and the level and analysis modules lose their silent
    broad excepts"). A 0 read as 0 in the S/R build's merge tolerance and
    the chart's HTF request, and as a reader's own default in the others
    (0.35 or 0.60 ATR, 0.003, 0.10 ATR, 0.0015), so one value meant 0, 0.35
    or 0.60 depending on the reader; a negative one read as it was, but as
    0 in the ladder spacing.

  `runtime.history_poll_seconds`, `risk.trailing_stop_pct` and the four
  `support_resistance` tolerances had checks of their own (the entries
  below), under which a quoted number read, and a negative trail read as
  off; in the table, a quoted one is refused like every other (`"300"`),
  and so is `true` / `false` (`true` read as 1), and the trail must be
  >= 0. A number YAML read as text, quoted or an exponent without a dot
  and a signed power (YAML 1.1 reads `5e6` as a string, `5.0e+6` as a
  number), is named with the number to write
  (`tradingview.min_volume must be an integer >= 0, got '5e6' (YAML read
  it as text, not a number: write 5000000)`); it was refused as
  `got '5e6'` with no hint why. The hint names only a number the check
  takes: an integer key's is written as an integer at any size (`1e16`:
  `10000000000000000`), and a number out of range (`'-5'` for a count)
  gets none, since unquoted it is refused too. A whole decimal where an
  integer belongs (`risk.max_positions: 2.0`), which loaded, is named with
  the integer to write (`risk.max_positions must be an integer >= 1, got
  2.0 (YAML read it as a decimal, not an integer: write 2)`), when the
  check takes it.

  The settings that name a mode are checked at load too
  (`config._CHOICES`): each takes only its documented values, spelled
  exactly so (`runtime.startup_reconcile_mode must be one of ['block',
  'ignore', 'log_only', 'restore_basic', 'restore_hybrid'], got 'blok'`).
  Until now each but the bracket modes read its values in any case, most
  with the spaces around them trimmed, so a spelling such as `Strict`,
  `NATURAL`, `EXTENDED`, `HTF` or the theme ` dark ` read as that mode or
  theme; and each fell back at runtime:
  - `runtime.startup_reconcile_mode`: lowercased, so any case read; a
    typo read as `log_only`, with a WARNING whenever the reconcile found
    broker positions or working orders (none when it found nothing or
    failed), which turned the blocking modes' entry block off, and a null
    as `ignore`;
  - `risk.trade_management_mode`: lowercased and stripped at load
    (`Adaptive` read as `adaptive`); a null read as `adaptive_ladder`
    without a word, and an unknown mode with a WARNING;
  - `risk.reentry_policy`: lowercased and stripped, with six undocumented
    aliases (`same_day`, `same-day`, `rest-of-day`, `session` and `day` for
    `rest_of_day`, `none` for `immediate`); a null read as `immediate`
    (through the `none` alias), and an unknown value as `cooldown` with a
    WARNING;
  - without a word: `runtime.equity_session_indicator_window`,
    `dashboard.charting.compact_chart_timeframe` and
    `support_resistance.order_block_mode` were lowercased and stripped
    (`EXTENDED`, ` htf ` and `Strict` read as those modes), and any other
    value read as `rth` / `ltf` / `loose`; `options.option_limit_mode` /
    `vertical_limit_mode` were lowercased (`NATURAL` priced at the
    natural side), and any other value priced at `mid`;
  - `dashboard.theme` must name a folder under `dashboard_assets/themes`
    named to `^[a-z0-9_-]{1,40}$`, a user's own included
    (`config.dashboard_themes()`); the dashboard server lowercased and
    stripped the name (`Nebula` or ` dark ` served that theme), and read a
    malformed one, or one naming no folder, as `default` with a WARNING (a
    null without one).

  The three `execution.bracket_*` modes, checked since the bracket orders
  landed, move into the same table with the same messages. The readers
  compare the value as it is: the reconcile's and the engine's
  `str(mode or "ignore").lower()` and the reconcile's unknown-mode branch,
  `RiskManager._normalized_reentry_policy`, the load's
  `_normalize_trade_management_mode`, `set_session_indicator_window`'s
  fallback, `DashboardChartingConfig.normalized_compact_chart_timeframe`
  and `dashboard._resolve_theme_name` are gone (the dashboard takes the
  theme-name pattern from `config`). The order-block mode is passed on as
  it is by the strategy base, the dashboard (its request to the feed and
  each block's label) and the feed (its cycle-cache key included), where
  each lowercased and stripped it or fell back to `loose`; the builder
  (`order_blocks.build_order_block_context`) and the empty context
  (`empty_order_block_context`, which the feed and the strategy base
  return with no bars) raise on any mode but `loose` / `strict`: the
  builder built loose blocks for one, and the empty context took it,
  lowercased and stripped, as its label. The option pricing helpers keep
  their `mode="mid"` default and lowercasing for direct callers.

  The config file itself is read as the event files are
  (`serialization.read_yaml`, where the calendar's line-naming loader
  moved): a file that is not YAML names itself, an unquoted date that
  cannot exist anywhere in it (an `events.earnings` date such as
  `AAPL: [2026-11-31]`) names its line and column instead of PyYAML's
  bare "day is out of range for month", and so does a value behind an
  explicit YAML tag that does not read as it (`!!bool maybe`, `!!int x`,
  `!!float y`, `!!timestamp 2026-9-30x`, `!!null 2026-09-16`, which
  PyYAML read as no value at all, and an unquoted `0x_`), which
  PyYAML failed with a bare KeyError, ValueError, IndexError or
  AttributeError naming neither the file nor the line (`<path>: not a
  readable YAML file: line 40, column 16: maybe is not true or false (a
  !!bool is true, false, yes, no, on or off)`). A file whose top level is
  not a mapping is refused (a list raised a bare AttributeError). So is an
  empty file, or one of comments only, naming the file (`<path>: the
  config file is empty; it must be a mapping of config sections`): it ran
  on the code defaults, a config the operator never wrote. And each
  section, `dashboard.charting` and its two profiles, and each
  `strategies` entry and its `params` must be a mapping, and `pairs` a
  list; a null one is empty (`config._section_shape_errors`). Each was
  read with `dict(value or {})`: `risk: 5` raised a bare "'int' object is
  not iterable" and `compact: [1]` a bare "cannot convert dictionary update
  sequence element", naming neither the section nor the file, and
  `strategies: {top_tier_adaptive: 5}` (or `null`) a bare AttributeError;
  a section left as `0`, `""`, `[]` or `false` read as an empty one, so
  every key in it ran on its default, a list of `[key, value]` pairs read
  as a mapping, and `pairs: {...}` as no pairs. A pair row must be a
  mapping with a `symbol` and a `reference` (`pairs[0] must be a mapping
  with a symbol and a reference, got 5`): one that was not was dropped
  without a word. A top-level key that names no section (`risks:`,
  `support_resistence:`) is refused, listing the sections: it was ignored,
  and every key under it ran on its default. Every one is named, with the
  file (`<path>: invalid config sections:` then `risk must be a mapping of
  settings, got 5`); the `strategies` check that raised a TypeError of its
  own is one of them.

  A key under `dashboard.charting` or its `compact` / `expanded` profile
  that none of them takes is refused, naming the key and listing the keys
  it takes (`dashboard.charting has an unknown key 'compact_timeframe': it
  takes compact_chart_timeframe, compact, expanded`). The load splits that
  section by hand, so a typo at its top level was ignored without a word,
  leaving the setting it meant at its default, and one in a profile raised
  the dataclass's bare "unexpected keyword argument". The retired `shared`
  profile and top-level `*_max_bars` keys, which had messages of their own
  that did not name the file, are unknown keys like any other.

  **Behaviour change:** each value above that is not a YAML number, not
  finite, out of its range, not `true` / `false` or not one of its mode's
  values refuses startup, naming the key; the checks run whether or not the
  feature reading the value is on. A mode or theme spelled in another case or
  with spaces around it (`Strict`, `NATURAL`, `EXTENDED`, `HTF`, `Nebula`, `
  dark `), which read as that one, is one of these, and so are a negative
  `risk.trailing_stop_pct` (it read as off), a 0 or negative
  `support_resistance` tolerance (above), a whole decimal where an integer
  belongs (`max_positions: 2.0`), an empty config file, an unknown
  `dashboard.charting` key, a value behind a YAML tag that does not read as it
  and a section that is not a mapping (above; one left as `0`, `""`, `[]` or
  `false` read as empty, a null one still does). The readers take the checked
  value as it is, so five readings change. `runtime.idle_sleep_seconds: 0`
  turns the idle cadence off, as documented (it read as 60, like a null).
  `options.debit_stop_time_decay_widen_factor: 0` is no widening in the
  post-fill levels too, of a single option and a debit spread alike (the entry
  gatekeeper read 0 as 0.30, the strategies as 0). `risk.peak_giveback_min_r`
  must be above 0 (a 0 read as 1.0; `peak_giveback_enabled: false` is the off
  switch, which the README now says instead of "set it to 0"). A chart
  profile's `max_bars` is drawn as configured: the clamp to 1-480, the default
  for an unreadable value and the switches read with `bool()` are gone with
  the check (`DashboardChartingConfig.resolved_profile` returns the profile).
  The dashboard's key-level zones read the strategy's level spec's tolerances
  as it gives them, as the chart's HTF request does, and
  `support_resistance`'s for a spec without them: a spec without them, or with
  an `htf_atr_tolerance_mult` / `htf_pct_tolerance` param of 0, which the
  strategy trades on as 0, read as 0.35 / 0.003. The readers lose their
  `getattr` defaults and `or` fallbacks for these keys (the 0DTE strategies'
  six option switches and three option times, microcap_pm_breakout's
  `risk.default_stop_pct`, the strategy base's `risk.trailing_stop_pct` and
  every reader of the four `support_resistance` tolerances included; the
  ladder spacing loses its clamp of a negative one to 0). All 19 presets load
  and build unchanged (tolerances 0.6, 0.003, 0.1, 0.0015). The README
  documents the checks (under "How the YAML is organized") and the changed
  readings; its `dashboard.charting` option reference gains the three profile
  options it left out (`show_fib_retracements`, `show_rsi_divergence`,
  `show_obv_divergence`) and no longer offers a `shared` profile.
  `dashboard_assets/README.md` documents the theme check, and
  `macro_events.example.yaml` lists `symbols` among a row's keys. Tests:
  `tests/test_config_validation.py` (`TestNumberAndSwitchChecks`, with a drift
  check that every number of these sections is listed and a load of every
  shipped preset, `TestTheReadersTakeTheCheckedValue`, `TestEnumChecks`, with
  a drift check that every string setting of these sections is a mode or free
  text, `TestDashboardTheme`, `TestChartingKeys`, with a drift check that the
  README's option reference lists every profile option, `TestNumberTextHint`,
  `TestConfigFileRead` and `TestSectionShapes`);
  `tests/test_sr_tolerance_reads.py` (new: every reader of the four tolerances
  on values that are none of the defaults, and a scan of the package for a
  fallback on them) and `tests/test_sr_ladder.py`; `tests/test_dashboard.py`
  (`TestThemes`, with the theme page read and the `/themes/` route's name
  check); `tests/test_order_blocks.py` (`TestTheModeIsTakenAsItIs`) and
  `tests/test_fix_dashboard_charting.py` (`TestOrderBlockMode`);
  `tests/test_risk_manager.py` loses the reentry alias tests;
  `tests/test_module_layering.py` lets `serialization` import `yaml`;
  `tests/test_warmup_tracker.py` and `tests/test_silent_excepts.py` lose the
  tests of the chart `max_bars` fallback.


- **A broken blackout or earnings file stops the bot at startup; edited
  while it runs, it keeps the rows last read.** *2026-09-26* — the
  calendar read `events.blackout_file` and `events.earnings_file` behind a
  broad `except`: a file that was not YAML, or could not be read, was a
  warning and no rows, so the bot ran with no blackouts. A scalar where a
  list or a mapping belongs (`events.blackouts: true`, a file whose top
  level or `events:` is a scalar) raised a bare TypeError or was ignored,
  and `symbols: NVDA` was read as the letters N, V, D, A, so the window
  blocked nothing. An earnings date not in a list (`AAPL: 2026-10-29`)
  raised a TypeError in every top_tier / small_cap_squeeze entry cycle,
  and an unparseable earnings date was dropped with a warning, so the
  symbol traded through its print. A `blackout_file` edited while the bot
  ran was not checked at all: a row missing a time was skipped, and a
  malformed one raised on the event's date, in the entry cycle and in the
  0DTE force-flatten check (the exit pass). Now:
  - at load, `events.blackouts` must be a list of mappings, and in each row
    `symbols` a list of tickers, `date` a `YYYY-MM-DD` date, `weekday` a
    weekday (`MON`-`SUN`, `MONDAY`-`SUNDAY` or `0`-`6`, in any case; its
    date's, when the row has both), `enabled`, `block_new_entries` and
    `force_flatten` `true` or `false`, and no key but those, `label`, `start`
    and `end` (`events.blackouts[0].date must be a YYYY-MM-DD date, got
    '2026-9-30'`, `events.blackouts[0] has an unknown key 'force_flaten': a
    row takes label, date, weekday, start, end, enabled, block_new_entries,
    force_flatten, symbols`); `events.earnings` must be a `{SYMBOL:
    [YYYY-MM-DD, ...]}` map whose symbols are non-blank strings and whose
    dates are `YYYY-MM-DD` dates (`events.earnings.AAPL[1] must be a
    YYYY-MM-DD date, got '2026-10-3O'`). A row's date is compared as a date:
    compared as text with today's ISO string, `2026-9-30`, `09/30/2026` or a
    YAML datetime never matched, and neither did a weekday that is not one
    (`FUNDAY`, `7`) or one its date does not fall on, so the window blocked
    nothing and its force flatten never came; `force_flatten: "false"` read as
    on; a typo'd key (`force_flaten: true`) was ignored, so the window never
    flattened; a `symbols` list holding only blank or null entries left a
    window that blocked nothing; an unquoted ticker YAML reads as a boolean
    (`ON`) never matched, and as an earnings key filed its dates under `TRUE`;
    and a blank earnings symbol was skipped. A date written as text must be
    `YYYY-MM-DD` itself, spaces around it aside: `date.fromisoformat` also
    read `20260930` (an integer to YAML, unquoted) and the week date
    `2026-W40-3`, and a weekday any word whose first three letters are one
    (`Monkey` as `MON`, `SATURN` as `SAT`, `Thurs` as `THU`), all refused now;
  - when the strategy is built, both files are read and checked the same
    way, and one that cannot be read or is not YAML is refused too
    (`invalid events.earnings_file:` then
    `earnings.yaml: not a readable YAML file: ...`, the path as it
    resolved). An unquoted date that cannot exist (`2026-11-31`), which
    PyYAML fails with a bare "day is out of range for month", is refused
    the same way, naming its line (`...: not a readable YAML file: line 3,
    column 10: 2026-11-31 is not a date (day is out of range for month)`),
    and so is a value behind an explicit tag that does not read as it
    (`!!timestamp 2026-9-30x`, `!!bool maybe`, `!!int x`, `!!float y`,
    `!!null 2026-09-16`),
    which PyYAML fails with a bare AttributeError, KeyError, ValueError or
    IndexError naming neither the file nor the line (the calendar read it,
    like a file that is not YAML, as a warning and no rows); the loader that
    names them (`serialization.LineNamingSafeLoader`) reads the config file
    too, so the same value inline is named the same way (see the entry
    above).
    The earnings file was first read in the first entry cycle; like the
    blackout file, it is now read at construction for every strategy;
  - a file edited while the bot runs is re-read on its mtime and checked
    the same way. A broken edit is logged once as an ERROR naming the file
    (`invalid events.blackout_file, keeping the rows last read from it:`),
    and the rows last read stay in force until the file is fixed; neither
    the entry cycle nor the force-flatten check raises on it.

  `sessions.parse_hhmm` refuses a float. YAML reads a dotted typo as one
  (`9.30` is 9.3, `9.00` is 9.0), and parse_hhmm truncated both to 00:09;
  every time check at load and construction now refuses it, naming the
  key. A window row whose `start` is `0` (00:00, which the load check
  passes) was skipped as falsy; it now applies.
  `scripts/sync_macro_events.py` wrote `events:` over `[]` when it found
  no events (every source down, or none inside the horizon), which is not
  YAML; it now writes `events: []`. `blackout_time_errors` is renamed
  `blackout_row_errors`, beside the new `earnings_errors`.
  **Behaviour change:** each fault above now refuses startup, even with
  `events.enabled: false` or a strategy that never consults the calendar,
  and a runtime edit carrying one is refused whole: the valid rows or
  dates in the same edit wait until it is fixed. A row dated with a YAML
  datetime (`2026-10-28 13:55:00`) now applies on that date, and so does a
  row whose quoted date has spaces around it (`" 2026-10-28 "`): both were
  compared as text with today's ISO string and never matched, so the
  window blocked nothing and its force flatten never came (an earnings
  date was read with its spaces trimmed already). An empty
  file, an `events:` / `earnings:` left empty, and a missing file (a
  warning) still hold no rows, at startup or later. A
  `macro_events.auto.yaml` that the sync script wrote with no events before
  this change refuses startup: re-run the script. No shipped preset or feed
  is affected: all 19 presets build, and the example files, `earnings.yaml`
  and the script's output read. Tests: `tests/test_sessions.py`,
  `tests/test_config_validation.py` (`TestOptionsValidation`,
  `TestEventsValidation`), `tests/test_top_tier_megacap.py`
  (`TestEventBlackouts`, with the 0DTE force-flatten end to end, and the
  row fields, the unknown keys, the symbols (lowercase ones scope too),
  the impossible dates and the values behind a tag that do not read at
  startup and on a runtime edit, a null weekday in a file row, a row dated
  ahead that neither blocks nor flattens before its date, a padded quoted
  date, each earnings session count on its own side of the date, and a
  lowercase earnings symbol filed in capitals) and the new
  `tests/test_sync_macro_events.py`.

- **The adaptive ladder's index re-check and the macro vote read the one
  posture test.** *2026-09-26* — the re-check
  (`PositionManager._ladder_indices_still_aligned`) and the VWAP branch of
  the peer family's macro vote (`_macro_signal`) call
  `indicators.bar_posture` instead of writing out close vs VWAP and EMA9 vs
  EMA20; the re-check still reads session VWAP. **Behaviour change:** in the
  re-check, an index bar with no EMA yet now has the close stand in for the
  missing EMA, as the entry side's `_frame_agrees` does on the same frame,
  so it can lean; the re-check read it as no lean, so the target exit fired.
  The feed leaves the last bar's EMA empty only for a frame of fewer than 20
  bars whose last bar is outside the indicator session, which on the shipped
  ladder presets takes an index whose every history fetch came back empty
  and whose stream stopped before the session, so live runs are unaffected.
  A bar with no close has no posture in both (the re-check read a missing
  close as 0.0, the macro vote a NaN one; the data feed carries neither).
  Tests: `tests/test_bar_posture.py` (the recorded SPY / AAPL / TSLA tapes
  read bar for bar as before).


- **Dead fallbacks and a posture copy removed: the 0DTE mark hint, the
  dashboard's LTF read, the vol_squeeze alignment.** *2026-09-26* —
  - The 0DTE debit, credit and long-option builders stamped
    `mark_price_hint` as the quoted mid x 100, else the legs' mid. The
    fallback could not run: the quoted mid comes from
    `_validate_spread_market` / `_validate_single_option_market`, which
    refuse a mid that is not above zero, and the price bounds floor every
    term at 0.0, so the mid is never NaN. The hint is now the quoted mid x
    100, and what was computed only for the fallback is gone:
    `net_debit_dollars` returns the debit alone, `net_credit_dollars` the
    credit and max loss, and `single_option_dollars` is removed.
  - `DashboardCache.strategy_level_zones` read the zones' LTF frame inside
    `except Exception: ltf = None`, beside an `elif` that resampled the
    passed frame when there was no data store, a case the method returns on
    at its top. Both are gone. **Behaviour change:** a failing LTF read drew
    the zones from the HTF candidates alone, sized on the HTF ATR, with no
    level marked for entry and nothing logged; it now raises out of the
    dashboard snapshot, as the snapshot's 1m and technical-frame reads
    always have: the cycle fails and is logged as an engine error, and a
    failure that persists stops the bot. No known input makes `get_merged`
    fail (empty, NaN or infinite bars, missing columns, any minute rule),
    and on every shipped preset that sets `ltf_minutes` the zones' read is
    the technical frame's own read, served from the cycle cache.
  - `_score_vol_squeeze`'s +0.5 VWAP/EMA alignment (top_tier_adaptive,
    small_cap_squeeze) was a hand-written copy of the posture test; it is
    now `indicators.bar_posture(...) == side` on the floats the entry loop
    resolved. It scores as before on every input the entry loop can hand
    it; only a direct call with a NaN EMA differs, reading it as the close,
    as the entry loop resolves one.

  No shipped preset or feed input scores, sizes or draws differently.
  Tests: `tests/test_zero_dte_risk.py` (`TestAValidatedMarketHasAMid`),
  `tests/test_zero_dte_shared_entry.py` (`TestTheMarkPriceHint`),
  `tests/test_fix_dashboard_charting.py` (`TestLevelZonesOnAnLtfReadError`)
  and `tests/test_vol_squeeze_regime.py` (`TestTheAlignmentBonusIsThePosture`:
  every order of the four inputs, ties and infinities included, against the
  old copy).


- **Unreachable branches, a flag that was never False and private copies of
  a `numeric` read removed.** *2026-09-26* —
  - Unreachable branches: `DashboardCache.strategy_level_zones` tested
    `strategy_obj is not None` twice (keeping an `else` for it) after
    returning at its top when there is no strategy, and read
    `dashboard_allow_generic_level_fallback` through `getattr` with a
    default though `BaseStrategy` defines it; top_tier's ORB 5m
    follow-through asked a DataFrame whether it has an `index` and `iloc`;
    the long-options builder refused an entry limit not above zero
    (`invalid_limit_price`), which `single_option_limit_price` cannot
    return (it prices at least a cent, a NaN quote included).
  - `SchwabExecutor._simulate_vertical_fill` /
    `_simulate_single_option_fill` lose `allow_natural_fill`: every caller
    passed True, so the dry-run reprice ladder always ends on the natural
    price, as it did. The 0DTE options README no longer names the flag.
  - The position manager's mark, the startup restore's current price and
    `DashboardCache.symbol_price` each looped over the quote's price fields
    with `float()` inside `except Exception`; each is now
    `numeric.first_float(..., positive=True)`, which reads every value the
    loop did the same way (+inf included). `PaperAccount.mark_prices` stores
    `safe_float(price)`: it stored `float(price)` unless that raised, a NaN
    included. The account-price fallback in `symbol_price` and the max-reward
    sum in the paper account's position rows lose a `try` that could not
    fail.
  - `DashboardCache.index_symbols` is the strategy's
    `dashboard_index_symbols`; it swallowed any error from it and re-read the
    params with a copy of the hook's union, which cannot raise.
  - `ZeroDteEtfOptionsStrategy._matching_event_blackout` had no caller.

  No shipped preset or feed input behaves differently. On inputs the bot
  cannot produce: a NaN handed to `mark_prices` keeps the last mark (it
  stored NaN), and an error from `dashboard_index_symbols` reaches the
  dashboard build. Tests: `tests/test_quote_price_reads.py` (each read
  against its old loop, on quotes of any shape), `tests/test_index_symbols.py`
  (`TestTheDashboard`), `tests/test_zero_dte_risk.py`
  (`TestALimitPriceIsAtLeastACent`) and `tests/test_runtime_nan_reads.py`
  (a SHORT row's max reward, untested until now).

- **67 silent broad excepts in the engine, dashboard, report, reconcile,
  position and strategy-base code are gone, narrowed or logged.**
  *2026-09-26* — each passed, continued or returned a default without
  logging anything.
  - 42 are removed: nothing in them could raise on the bot's own frames,
    quotes, records or validated config, so an error there now raises
    instead of reading as a default.
  - 12 are narrowed to the errors their code raises and keep their
    fallback: the TLS close and the `bars` query parameter, the FVG anchor
    stamp, the snapshot JSON signature, `fmt_metric`, the key-levels
    thresholds and weight, the context cache keys, and the archive's log
    flush. The option chain's `date:dte` key now reads through `partition`
    and `safe_int`, with the same results as before.
  - 6 wrap strategy hooks or analysis builders and now log a rate-limited
    WARNING with the traceback: the dashboard's HTF and LTF fair value
    gaps, the level-zone candidates and width, the candle context, and the
    exit record's S/R row. The S/R row is built before the exit order is
    sent, so it stays guarded.
  - 1 is the session archive's read of each symbol's merged frame at each
    timeframe. It now logs a WARNING with the traceback (`Could not read
    the merged frame for MSFT/15m: ...`) and leaves that symbol out of that
    timeframe's bars, counted in the manifest's `bars_skipped_by_timeframe`;
    the rest of the archive is written. Like the HTF-frame read beside it,
    it runs once per export and is not rate-limited.
  - 2 are the adaptive ladder's persisted active-index and rung reads. They
    are narrowed and report an unreadable value once a minute; removing
    them would have stopped the ladder managing that position.
  - 3 moved to load time: the two below, and the chart profiles'
    `max_bars` (see "A blank or malformed time, number, switch or mode stops
    the bot at startup"); 1 is the restore universe (see Fixed), which now
    fails closed.

  The 41 in the other 18 modules are covered by the two entries that follow.

  **Behaviour change (config): two values that were accepted silently now
  refuse to start.**
  - `runtime.cycle_precompute_workers` must be an integer of at least 1.
    Before, 0, null or a typo ran 4 workers and a negative count ran 1.
  - `risk.trailing_stop_pct` must be a finite number >= 0 (a YAML number,
    not quoted) or null; 0 or null turns the trail off. Before, a typo
    turned the trail off at every entry and restore, and so did a negative
    value, and an infinite value kept an infinitely wide trail.

  Every shipped preset loads unchanged: 4 workers, trail 0.012-0.12. A
  non-numeric `min_bars` or `ladder_*` param now raises like every other
  strategy param read, instead of reading as 0 or the default. The README
  documents both checks and the archive's skipped symbol
  (`export_session_archive`), and `_strategies/README.md` documents the
  restore universe. Tests: `tests/test_silent_excepts.py` (the archive's
  frame read in `TestReportedArchiveFrameRead`),
  `tests/test_config_validation.py`.

- **The data feed, the S/R ladder spacing and the level and analysis
  modules lose their silent broad excepts.** *2026-09-26* — 24 more
  `except Exception:` handlers that passed, continued or returned a
  default without logging anything:
  - 17 are removed: nothing in them can raise on the bot's own frames,
    quotes, stamps or validated config. Among them are the quote age and
    freshness reads (`fetch_quotes` stamps every quote with the ET clock),
    the completed-bar cut of the HTF and fair-value-gap builds (on an error
    it returned the frame without its last bar, a completed one), the gap
    anchor parse, the reference price and the order blocks' last close, the
    chart patterns' close / open reads and indicator-frame check, a TA-Lib
    candle signal, the channel slope check, the ladder's level price, the
    HTF refresh's incremental start (with its tz-naive branch, which no
    stored frame reaches) and the feed's own `cycle_precompute_workers`
    read, validated at load since the entry above.
  - 5 moved to load time (below): the quote blacklist's count and the four
    tolerances the ladder spacing reads.
  - The two TA-Lib import guards (`indicators`, `candles`) catch only
    ImportError. A missing TA-Lib, or a missing C library, still fails
    where TA-Lib is first used, and the error now says why (`TA-Lib is
    required for indicator calculation but could not be imported:
    libta_lib.so.0: ...`, with the ImportError as its cause); it said "is
    not installed" and dropped the import error. Any other import failure,
    such as a build against another numpy, now fails at startup instead of
    reading as "not installed".
  - Not changed: the quote fetch's two per-symbol handlers, which log a
    WARNING per failure through `_record_failure`.

  **Behaviour change (config): five values that were read silently now
  refuse to start.**
  - `runtime.max_consecutive_quote_failures` must be an integer of at
    least 0 (0 turns the blacklist off). Before, null or a negative count
    turned it off, a float was truncated, `"5"` read as 5, `true` as 1 and
    a typo as 5.
  - `support_resistance.atr_tolerance_mult`, `pct_tolerance`,
    `same_side_min_gap_atr_mult` and `same_side_min_gap_pct` must be
    finite YAML numbers above 0 (not quoted, not `true` / `false`: they are
    in the load check's number table, see "A blank or malformed time,
    number, switch or mode stops the bot at startup", where the readings
    of a 0 or a negative one are listed), even with `enabled: false`.
    The S/R and HTF builds and the dashboard read them with `float()`, so
    a typo already failed every build; the ladder spacing (sr_flip
    management and the dashboard's ladder) read it, or a null, as the
    default. An infinite or NaN value reached the builds.

  Every shipped preset loads unchanged (blacklist 5; tolerances 0.6,
  0.003, 0.1, 0.0015). On inputs the bot does not produce, an error in a
  removed handler now raises instead of reading as a default: a quote
  stamp that is not a time (it read as no age, and as stale), an index
  that is not times (the completed-bar cut dropped the last bar), a last
  close or a ladder level price that is not a number (a 0 reference price;
  the level skipped), a gap anchor that does not parse (the gaps kept
  apart). The README documents both load checks. Tests:
  `tests/test_silent_excepts_data_levels.py` (new),
  `tests/test_config_validation.py` (`TestRuntimeValidation`,
  `TestSupportResistanceValidation`) and `tests/test_sr_ladder.py`.

- **15 silent broad excepts in the logging, persistence, symbol, warm-up
  and shared entry / exit code are gone, narrowed or logged.**
  *2026-09-26* — each passed or returned a default without logging
  anything.
  - 7 are removed, because nothing in them raises on the bot's own inputs:
    the shared entry stage's S/R proximity term (the entry gates read the
    same clearance with no guard), the shared exit policy's hold time (the
    exit record measures the same hold with no guard; its fallback of 0
    kept the ORB and structure graces open and the time stop off),
    volatility_squeeze's volume / width median, the Windows console
    colour setup (every failure there is a checked return value), the
    warm-up snapshot's history retry timing (its one input is checked at
    load, below), `log_structured`'s second fallback, and the stored
    position metadata read (see Fixed). An error in the hold time now
    skips that position's shared and strategy exits for the cycle, and
    its stop, target and force flatten still fire (see "One position's
    management error no longer skips the positions after it" under
    Fixed).
  - 4 are narrowed and keep their fallback. `_json_ready` writes a value as
    its string only on the three errors a number type raises from
    `float()` (`TypeError`, `ValueError`, `OverflowError`). The FVG recency
    decay reads a stamp `pd.Timestamp` cannot parse as having no age, and
    only that. With no `try` at all: an exit reason's trigger level reads
    through `safe_float`, so `stop:nan` is no level (it was a NaN, written as
    a bare `NaN` in the exit line). `normalize_symbol_list` still reads a
    non-iterable (a YAML `5`) as no input, but an iterable that raises
    while it is read now raises.
  - 1 moved to load time: the warm-up tracker's chart requirement is now
    the expanded chart's `max_bars`, which the config load checks (an
    integer from 1 to 480; see "A blank or malformed time, number, switch
    or mode stops the bot at startup"). It read the raw value: above 480 it
    requested bars no chart draws, and a typo requested none. Every shipped
    preset (360) requests the same bars as before.
  - 3 stay broad, each at a boundary that must not fail its caller, and now
    log the first failure with its traceback: `_json_ready`'s last fallback
    (`<unserializable TYPE>`, once per type), `log_structured`'s
    `serialization_error` line (once per prefix; the callers are the entry
    and exit flows), and the warm-up tracker's `required_history_bars` hook,
    which runs before the cycle's position management (once per strategy;
    the symbol still reads as needing no warm-up bars).

  **Behaviour change (config): `runtime.history_poll_seconds` must be a
  finite number above 0.** The feed's history refresh check and the warm-up
  retry timing read it with `float()` ahead of position management. Before,
  an unreadable value raised there every cycle, an infinite one never
  refreshed history, and 0 or less refetched every cycle. It now refuses to
  start, naming the key, and so does a quoted number, like every number
  the config load checks. Every shipped preset has 300. The README documents
  the check and the chart requirement. Tests: `tests/test_audit_logger.py`,
  `tests/test_log_setup.py`, `tests/test_reasons.py`,
  `tests/test_position_store.py`, `tests/test_symbols.py`,
  `tests/test_warmup_tracker.py`, `tests/test_config_validation.py`,
  `tests/test_shared_entry_policy.py`, `tests/test_shared_exit_policy.py`.

- **The dashboard's key-level zones use the strategy's ATR read.**
  *2026-09-26* — `DashboardCache.strategy_level_zones` sized its zones with
  a hand-rolled copy of the ATR read in key_levels' `_select_level`. It now
  makes the same call,
  `indicators.last_bar_atr(ltf, close, fallback_atr=htf.atr14)`, so the
  overlay can no longer drift from the strategy. The copy read a zero,
  negative or -inf LTF `atr14` as max(0.15% of the close, $0.01), kept
  +inf, and kept a +inf HTF ATR when the LTF had no reading; the strategy
  uses a finite reading as read and falls back to the HTF ATR, when that is
  finite and positive, for an infinite one. **Behaviour change:** only on
  those readings, which no shipped feed produces: the ATR of finite bars is
  finite and never negative, and 0 only when every bar since the ATR seed
  is flat; the HTF ATR is floored at $0.01. On such a reading +inf drew
  every zone from $0 to +inf (sent to the page as null), and at $100 with an
  HTF ATR of 1.25 -inf drew key_levels' zone 0.16 wide instead of the 0.275
  the strategy selects with. A zero reading leaves the key_levels,
  htf_pivots and top_tier widths as they were (their percent floors bind);
  trend_continuation's trigger zone drops from 0.03 to its $0.01 floor.
  Tests: `tests/test_last_bar_atr.py`
  (`TestTheDashboardZonesReadTheStrategysAtr`, and the new site in
  `EVERY_READ`).


- **The entry cycle summary reads reasons through `reasons.py`.**
  *2026-09-26* — `EntryGatekeeper._decision_reason_key`, a fourth reason
  parser, is gone. `ENTRY_CYCLE_SUMMARY`'s `top_skip_reasons` now tallies a
  skip under the new `reasons.reason_gate`,
  `exit_reason_code(reason_head(rest))` where `rest` is the reason without
  its side prefix; the session report's filter rejections and gate
  attribution read the same key (see Fixed). The new
  `reasons.split_side_prefix` reads that prefix back (`long.no_setup` ->
  `(Side.LONG, 'no_setup')`), the reverse of `side_prefixed_reason`, whose
  prefix format the two now share. Every reason the bot builds keeps its
  key. Only malformed reasons, which nothing produces, bucket differently:
  a blank before the `(` or `:` or after the side prefix is stripped
  (`x (y)` -> `x`, was `x `), and a reason with no name before its `(`
  keeps itself as its key (`(x=1)`, was `none`), as `reason_head` reads it.
  Tests: `tests/test_reasons.py` (the old key kept as an oracle) and
  `tests/test_entry_cycle_summary.py` (the summary itself).

- **One posture test; the peer family reads one session open (refactor cut
  B20).** *2026-09-26* — `indicators.bar_posture(last, reference=None)`
  replaces top_tier `_frame_agrees`' and the peer vote's copies; a bar with
  no close has no posture (top_tier read a NaN close as 0.0, a SHORT lean;
  the peer vote used the 1m close; the data feed drops NaN-close bars, so
  live runs are unaffected). **Behaviour change:** the macro vote's no-VWAP
  fallback (index symbols without volume: NYICDX and VIX in every peer
  preset) reads today's RTH open, the one the peer vote uses
  (`_session_open`), instead of the first bar of the frame's last date:
  after 09:30 on a frame with premarket bars it measures from 09:30, and
  with no bar today the term cannot vote.

- **The peer family runs one side template (refactor cut B12).** *2026-09-26* —
  htf_pivots and trend_continuation evaluate their sides through the family
  base: `_preferred_sides`, `_evaluate_sides`, `_pick_side_signal`
  (`rank_key`, then the screener's side). One `_macro_allows` (from the
  params; trend_continuation read the context's `enabled` stamp, set from the
  same param) and one failure key, `_failure_style_name`. key_levels' macro
  gate calls `_macro_allows` then its net-bias check; its level-first loop,
  side pick and disregard of `directional_bias` are unchanged. **Behaviour
  change:** trend_continuation skips a candidate outside a non-empty
  `params.tradable` as `symbol_not_tradable`, before the position check, as
  the other two do. Its screener emits only `params.tradable` symbols, so
  live runs are unaffected; such a candidate can no longer take a
  divergence-only entry.

- **One last-bar ATR read for the strategy layer (refactor cut B7).**
  *2026-09-26* — `indicators.last_bar_atr(frame, close, *, fallback_pct=0.0015,
  floor_pct=None, floor_abs=0.01, fallback_atr=None)` replaces
  `BaseStrategy._frame_atr14`, `SharedEntryPolicy._last_atr` and the
  hand-rolled copies in top_tier_adaptive, the peer strategies,
  volatility_squeeze_breakout, `_entry_exhaustion_reasons` and the divergence
  entry. A last bar with no ATR (fewer than 15 bars of warm-up, a NaN) now
  reads like a missing column: max(0.15% of the close, $0.01). `_frame_atr14`
  (the refinement clamp, the retest stop anchor, the technical-exit buffer,
  the divergence ladder) read it as 0.15% of the close alone, under a cent
  below $6.67, so on small_cap_squeeze and microcap names without an ATR yet
  the `min_stop_atr_mult` floor and those buffers now use $0.01. Every other
  site passes the floor it had and reads the same. An infinite ATR reads as
  missing too (the same NaN-trap rule as the numeric fixes).

- **`_strategies/helpers.py` is gone; symbols have their own module,
  `symbols.py` (refactor cut C24).** *2026-09-26* — `symbols.py` holds the
  feed's symbol tables (`STREAMABLE_EQUITY_RE`, `NON_STREAMABLE`,
  `SR_SYMBOL_ALIASES`, `MARKET_INTERNAL_SYMBOLS`, `QUOTE_SYMBOL_ALIASES`),
  the four classifiers that were `MarketDataStore` static methods
  (`is_streamable_equity`, `normalize_context_symbol`,
  `is_market_internal_symbol`, `is_support_resistance_symbol`) and
  `normalize_symbol_list` / `normalize_symbol_list_details`, which replace
  the helpers pair and three private copies: config's
  `_normalize_symbol_tokens`, `DashboardCache._normalize_symbol_list` and
  the peer family's `_dedupe_symbols`. Every list reads as before; the
  dashboard's lists now also accept a frozenset or a generator (none is
  passed today). The option premium clamps are
  `options_mode.clamp_long_premium_levels` / `clamp_short_premium_levels`;
  the positive-quote read is `numeric.first_float(..., positive=True)`; the
  zone-width policy and the position/strategy-name match are `BaseStrategy`
  static methods; `_long_option_style_gate` moved from the 0DTE base to
  `zero_dte_etf_long_options`, its only user, and its
  `_long_option_style_enabled` twin of the inherited `_style_enabled` is
  gone. There is no alias: `data_feed.NON_STREAMABLE` and
  `MarketDataStore.is_streamable_equity` & co. are gone, import from
  `intraday_tv_schwab_bot.symbols`. No behaviour changes beyond the Fixed
  entry for `options.underlyings`.

- **The strategy layer reads numbers through `numeric` too (refactor cut
  C23).** *2026-09-26* — `_strategies/helpers.py`'s `_safe_float`,
  `_optional_float` and `_optional_int` are gone. Every strategy module,
  `shared_entry`, `shared_exit` and `strategy_base` call
  `numeric.safe_float(value, 0.0)` where `_safe_float` defaulted to 0.0, and
  `numeric.safe_float(value, default)` / `numeric.safe_int(value, default)`
  elsewhere. `_is_scalar_missing` is private to `reasons.py`, and the 0DTE
  `ambiguous_regime` / `no_style_trigger` reasons compute their score gap and
  ORB triggers with `safe_float`. The scaffold templates and the plugin
  README example import `safe_float` from `numeric`. The readings are
  unchanged except for inputs no shipped preset or feed produces:
  - a string spelling NaN (`'nan'`; PyYAML reads a bare `nan` as one) is now
    the default instead of NaN;
  - `_safe_float` passed its default through `float()`, and every call
    passes a float default anyway;
  - a `float()` that raises something other than TypeError / ValueError /
    OverflowError now propagates.

- **Bar geometry, session slices and bucket completion live in `bars.py`
  (refactor cut C22).** *2026-09-26* — `bar_close_position`,
  `bar_wick_fractions`, `bars_have_range`, `CANDLE_PATTERN_WINDOW_BARS`,
  `same_day_mask`, `time_gte_mask` and `session_open_price` moved out of
  `_strategies/helpers.py` under public names, and `chart_patterns`' own
  `_bar_close_position` copy is gone. `session_open_price` takes the `day`
  (it no longer reads the clock), and the 0DTE strategy's RTH-then-premarket
  retry is `fallback_to_premarket_on_nan=True`. `bar_closed_after` moved out
  of `shared_exit`. `last_bucket_forming` / `completed_bucket_mask` are the
  one "is this bucket still trading" test (strategy_base, the dashboard,
  data_feed, htf_levels). `rth_open_plus` and
  `indicators.indicator_session_start` replace the 09:30 literals and the
  extended-vs-RTH start rule. `sessions.is_time_in_window` reads its ends
  like `parse_hhmm` and replaces the two `_time_in_range` copies and the
  macro blackout's inline check. There is no alias. No behaviour changes on
  any input the bot produces (the string `'nan'` now reads as missing; a
  naive frame compared with the clock is read on the ET wall clock).

- **trend_continuation's extension hard cap refuses under its own tokens
  (refactor cut B19).** *2026-09-26* — past `max_extension_from_vwap_atr` /
  `max_extension_from_ema9_atr` x `extension_hard_cap_mult` the side is now
  refused as `too_extended_hard_cap_vwap_atr` / `too_extended_hard_cap_ema9_atr`.
  It used the base exhaustion filter's `too_extended_from_vwap_atr` /
  `too_extended_from_ema9_atr` (`max_entry_*_extension_atr`), a separate check
  with its own thresholds, so a refusal could list one token twice with two
  thresholds, and the session report's filter rejections and gate
  attribution counted both checks as one gate. The thresholds and what is
  refused are unchanged; only the reason strings (and so those report
  buckets) change, and a comparison across this date has to add the
  hard-cap buckets (`too_extended_hard_cap_*`, and `wrong_side_hard_cap_*`
  from the renaming below) back to the base filter's. With the shipped
  preset the base filter's lower thresholds (0.95 / 0.75 ATR against the
  caps' 1.52 / 1.28), on the same ATR, always fire with a
  `too_extended_hard_cap_*` token, so that token appears without the base
  filter's only when the base filter is off. The cap measures the absolute
  distance, so it also refuses a close that far past VWAP / EMA9 against
  the side, where the base filter, which measures only the side's own
  direction, is silent; since the renaming of the same date (below) that
  refusal is `wrong_side_hard_cap_vwap_atr` / `wrong_side_hard_cap_ema9_atr`.

- **trend_continuation's extension hard cap names a wrong-side refusal.**
  *2026-09-26* — the hard cap (`max_extension_from_*_atr` x
  `extension_hard_cap_mult`) reads the close's absolute distance from VWAP /
  EMA9 in ATR, so it also refuses a LONG that far below the line and a SHORT
  that far above, and it called that refusal `too_extended_hard_cap_vwap_atr`
  / `too_extended_hard_cap_ema9_atr`, as if the side had run away in its own
  direction. It is now `wrong_side_hard_cap_vwap_atr` /
  `wrong_side_hard_cap_ema9_atr`, with the same distance and threshold in
  its fields; a close stretched past the line on the side's own side (a
  LONG above, a SHORT below) keeps `too_extended_hard_cap_*`. What is
  refused is unchanged: the cap stays on the absolute distance, the
  strategy's only hard refusal of a close far on the wrong side of VWAP /
  EMA9 (`below_vwap`, `above_ema9` and the other trend checks only cost a
  score point), and `extension_penalty_per_atr`'s penalty and
  `execution_headroom_score` read the same distance as before. Only the
  reason strings change, and with them the session report's
  filter-rejection and gate-attribution rows and the cycle summary's top
  blockers: a comparison across this date adds `wrong_side_hard_cap_*` back
  to `too_extended_hard_cap_*`. Replaying the shipped preset on the AAPL /
  SPY / TSLA fixture tapes (every entry-window minute of 2026-04-15/16, both
  sides: 4,542 side evaluations), the cap refused the same 2,416
  side-minutes before and after, 1,208 per side, with the same reasons once
  the new heads are read as the old and the same scores, penalties,
  headroom and signals; 1,208 of them (969 SHORT, 239 LONG) are now
  `wrong_side_hard_cap_*`, and no refusal mixes the two. Every
  `too_extended_hard_cap_*` token came with the base exhaustion filter's
  `too_extended_from_*_atr` for its line, and no `wrong_side_hard_cap_*`
  token did. Tests: `tests/test_peer_shared_entry.py`
  (`TestTheExtensionHardCap`).

- **Reason strings have one home, `reasons.py` (refactor cut C21).**
  *2026-09-26* — `reason_with_values`, `insufficient_bars_reason`,
  `detail_fields`, `fmt_metric`, `bool_token` and `side_prefixed_reason(s)`
  moved out of `_strategies/helpers.py` under public names (the leading
  underscore is gone). Two readers replace five copies. `reason_head` (the
  text before the first `(`, stripped, or the whole reason when nothing
  precedes the `(`) replaces the entry stage's `_reason_prefix` and the
  session report's `_normalize_skip_reason`. `exit_reason_code` (the text
  before the first `:`, stripped, or None) replaces the report's
  `_exit_reason_bucket`, its inline stop-exit test and the code half of
  `position_metrics.exit_reason_details`. Every reason the bot builds reads as
  before; only a malformed exit reason (an empty code, or a blank before the
  colon) now gets a stripped code or None in the exit payload. The peer
  family's `_gate_snapshot`, `_score_threshold` and `_discrete_score_threshold`
  moved to `peer_confirmed_key_levels/strategy.py`, and the 0DTE formatters
  `_style_unavailable_reason`, `_ambiguous_regime_reason` and
  `_no_style_trigger_reason` to `zero_dte_etf_options/strategy.py`.
  `_strategies/__init__.py` is now only a docstring and re-exports nothing,
  which reverses the "`_strategies.insufficient_bars_reason` promoted to
  public API" note under Added: the warm-up tracker imports it from
  `reasons`. There is no alias or stub. `tests/test_reasons.py` keeps the old
  parsers as oracles.

- **`_strategies/shared.py` is gone: every module imports a name from the
  module that defines it (refactor cut C20).** *2026-09-26* — the hub
  re-exported 95 names (18 of them imported by nobody) and gave the strategy
  layer a second import route beside the origin modules that `shared_entry`,
  `shared_exit` and half of `strategy_base` already used. The strategies,
  screeners and `strategy_base` now import the domain types from `models`,
  the session names from `sessions`, the analysis contexts from `candles`,
  `chart_patterns`, `support_resistance`, `htf_levels` and
  `technical_levels`, `resample_bars` from `bars`, the indicator helpers from
  `indicators`, the option builders from `options_mode`, the pure strategy
  helpers from `_strategies/helpers.py`, and the standard library and pandas
  directly. There is no alias or stub. `strategy_base`, the 0DTE ETF options
  strategy and five screeners (opening_range_breakout, microcap_gap_orb and
  the three peer_confirmed ones) logged through the hub's logger; each now
  logs under its own module name (for example
  `intraday_tv_schwab_bot._strategies.strategy_base` instead of
  `intraday_tv_schwab_bot._strategies.shared`), so a log filter on the old
  name must change. The plugin scaffold and the `_strategies/README.md`
  examples import the same way, and `tests/test_shared_knob_contract.py`
  now imports every name they use (its AST scans never did, so a template
  importing a deleted module passed). `tests/test_module_layering.py`
  rejects a module that imports another module's `LOG`. No behaviour changes.

- **The plugin registry is two modules, `catalogue.py` and `factory.py`
  (refactor cut C19).** *2026-09-26* — `_strategies/catalogue.py` reads and
  validates the manifests and looks plugins up (`get_plugins`, `get_plugin`,
  `plugin_names`, `normalize_strategy_name`, `default_strategy_name`,
  `option_strategy_names`, `is_option_strategy`, and `plugin_key`, the
  lower-cased name key, which was the private `_normalize_name`); it imports
  no plugin module. `_strategies/factory.py` imports a plugin's classes and
  builds them (`build_strategy`, `build_screener`,
  `normalize_strategy_params`), importing `BaseStrategy` and
  `BaseStrategyScreener` at the top instead of inside the loaders.
  `registry.py` is gone and there is no alias: import from the two new
  modules. The `_strategies` package no longer re-exports the registry
  functions, `StrategyManifest`, `BaseStrategy` or `BaseStrategyScreener`
  (nothing used them); `insufficient_bars_reason` stays for now.
  `BaseStrategy.__init__` looks its manifest up with no fallback, so a
  hand-built config naming an unknown strategy now raises `ValueError`
  instead of building a strategy with no manifest capabilities (`load_config`
  and `build_strategy` already rejected such a name). The engine, entry
  gatekeeper, startup reconciler, warmup tracker and position manager import
  `BaseStrategy` for type annotations only. Importing `config` now loads the
  strategy base (and with it `shared_entry`), which the first `load_config`
  used to do. The runtime import graph has no cycles. No other behaviour
  changes.

- **The two support/resistance config helpers are `SupportResistanceConfig`
  methods (refactor cut C18).** *2026-09-26* — `config.flip_confirmation_bars(sr)`
  and `config.htf_structure_event_lookback(sr)` are now
  `SupportResistanceConfig.flip_confirmation_bars()` and
  `.htf_structure_event_lookback()`; callers read
  `config.support_resistance.flip_confirmation_bars()`. They were the only
  reason a strategy module imported `config` at runtime (both peer plugins,
  and a function-local import in `BaseStrategy._structure_event_recent`), so
  the strategy layers now import `config` for types only and `config` is out
  of the runtime import cycle; `tests/test_module_layering.py` drops the three
  allowlisted imports and the cycle shrinks to registry <-> strategy_base.
  `dashboard_cache` imports `BotConfig` with its other config names instead of
  under `TYPE_CHECKING`. `_structure_event_recent` reads
  `self.config.support_resistance` directly (a config without that section
  used to fall back to a 6-bar HTF window; every `BotConfig` has one). No
  behaviour change.

- **`VETO_GATES` lives in `_strategies/plugin_api.py` (refactor cut C17).**
  *2026-09-26* — the closed set of vetoes a manifest's
  `capabilities.shared_entry.exemptions` may name is manifest vocabulary, so
  it moved out of `shared_entry.py` next to `StrategyManifest`. The registry
  imported the whole entry stage only to validate those lists. Import it from
  `intraday_tv_schwab_bot._strategies.plugin_api`: `shared_entry` does not
  re-export it, and the knob-contract test no longer lets a strategy import
  it from there. No behavior change.

- **Every level builder floors its ATR the same way (refactor cut B6).**
  *2026-09-26* — `indicators.atr_with_floor(frame, price, *, floor_pct=0.0015,
  abs_floor=0.0)` = max(the frame's current ATR, `price` x 0.15%,
  `abs_floor`) replaces `atr_value` and the three hand-rolled
  "floor only a missing ATR" copies. Support/resistance and the technical
  levels are unchanged (the frame's last close, no absolute floor). The HTF
  context, the fair-value-gap detector (HTF and LTF) and the order blocks
  now floor at 0.15% of the live price and $0.01 even when an ATR exists:
  before, a quiet name's ATR under 0.15% of price was used raw. At the
  shipped settings this moves the published HTF `atr14` on quiet frames
  (the SPY 15m snapshot's `atr14` goes from 0.52 to 1.05); the FVG size is
  unchanged above $1 because its percent term already won, and order blocks
  (off in every preset) measure thrust on the floored ATR. On a name whose
  HTF ATR is under a cent the $0.01 floor also moves the HTF `level_buffer`
  (below $2.50) and the level-clustering tolerance (below about $1.17-$2,
  by the tolerance multiple), so the published HTF supports / resistances of
  such names can change. Most strategies take entries and stops from the
  support/resistance context, which is unchanged, but peer_confirmed_key_levels
  (and the presets built on it) picks its entry levels, stops and target
  rungs from the HTF context, so on such names those can move too.

- **One numeric coercion, `numeric.py` (refactor cut C15).** *2026-09-26* —
  `safe_float(value, default=None, *, finite=False)`, `safe_int(value,
  default=None)` and `first_float(mapping, *keys, default=None,
  positive=False, finite=False)` replace the private copies:
  `position_metrics.safe_float`, `SchwabExecutor._safe_float` / `_safe_int` /
  `_quote_number`, `dashboard_cache.dashboard_safe_float`,
  `candles._safe_float_token`, the paper account's `_opt_float`,
  `MarketDataStore._safe_stream_float` (now `finite=True`) and the body of
  `RiskManager._signal_entry_price`. None, blank strings, NaN (the string
  `'nan'` included), `pd.NA` / `NaT` and unparseable values read as the
  default; ±inf passes unless `finite`. The inputs that read differently
  cannot reach these sites: an int too large for a float (now the default,
  not an OverflowError), the string `'nan'` in the candle cache key
  (`pd.to_numeric` + `dropna` remove it first), a quote or signal metadata
  that is not a dict, and a value whose `float()` raises something other
  than TypeError / ValueError / OverflowError (the catch-all copies returned
  None; it now propagates). `tests/test_numeric.py` pins the semantics and
  the sites whose copy had a shape of its own.

- **`utils.py` is gone: bar frames live in `bars.py`, indicators in
  `indicators.py` (refactor cut C14).** *2026-09-26* — `bars` holds
  `ensure_ohlcv_frame`, `floor_minute`, the session bucket grid
  (`session_bucket_bounds` / `_floor` / `_ends`), `resample_bars`,
  `frame_bar_minutes`, `equity_stream_window_bars` and
  `resolve_current_price`. `indicators` holds the process-wide
  session-indicator mode and its setters, the standard indicator frame
  (`ensure_standard_indicator_frame`, `has_standard_indicator_columns`,
  `STANDARD_INDICATOR_COLUMNS`), EMA span scaling, the TA-Lib
  wrappers, the session stitch and masks, `latest_atr14`, `atr_value` and
  `add_indicators`. `build_schedule` moved to `config`, and
  `append_management_adjustment` to `position_metrics` (the position
  manager's `_append_adjustment` alias is gone). Every piece moved verbatim;
  there is no alias or stub, so import from the new modules.
  `resolve_current_price`'s debug line now logs under
  `intraday_tv_schwab_bot.bars`. No behaviour changes.

- **Logging setup has its own module, `log_setup.py`, and the atomic file
  write is in `serialization.py` (refactor cut C13).** *2026-09-26* —
  `setup_logging`, `TRADEFLOW_LEVEL`, the ET-dated daily file handler and the
  colour console formatter moved out of `utils.py`. `log_setup.warn_once(key)`
  (one process-wide set) replaces the private warn-once copies in
  `_strategies/rvol.py` and `screener_client.py`. `atomic_write_text` moved to
  `serialization.py`, and `DashboardServer.publish` writes its state file
  through it instead of an inline copy. There is no alias: import them from
  `intraday_tv_schwab_bot.log_setup` / `.serialization`. The startup
  "Logging to …" line now logs under `intraday_tv_schwab_bot.log_setup`. The
  unused `utils.TRADEFLOW` alias, `logging.TRADEFLOW` and `Logger.tradeflow()`
  are gone; the level name is still registered on import.

- **Session masks are vectorized (refactor cut C12).** *2026-09-26* —
  `sessions.session_mask(index, window)` ("rth" or "extended") replaces the
  per-bar `equity_session_state` loop behind `utils.indicator_session_mask`
  and `levels_shared._rth_bar_mask` (the prior day / week filter), and
  `indicator_session_open` reads the clock through the same mask.
  `sessions.rth_close_minute(dates)` is the one early-close lookup, shared
  with the bucket grid. Every index gets the same bars as before, each read
  on its own wall clock (naive is ET); `tests/test_session_mask.py` keeps the
  old loop as its oracle. About 10x faster: a 3,900-bar 1m frame's mask went
  from 14.6 ms to 1.3 ms, and `add_indicators` from 54 ms to 35 ms.
  `_indicator_session_predicate` is gone.

- **The session calendar and helpers live in `sessions.py` too (refactor cut
  C11).** *2026-09-26* — `parse_hhmm`, the NYSE holiday and early-close
  calendar (`us_equity_market_holidays`, `us_equity_early_close_days`,
  `is_weekday_session_day`), the `EQUITY_*` session times,
  `EquitySessionState` / `equity_session_state` and its wrappers
  (`is_regular_equity_session`, `is_equity_stream_session`,
  `classify_equity_session`, `classify_tradingview_market_session`,
  `equity_rth_open_at`, `equity_rth_close_at`, `previous_regular_close`) and
  the `is_time_in_window` comparator moved out of `utils.py`, and `datetime_index`,
  `session_datetime_index`, `session_segment_ids` and `latest_session_date`
  out of `levels_shared.py`. There is no alias: import them by name from
  `intraday_tv_schwab_bot.sessions`. `_strategies.shared` still re-exports
  `parse_hhmm`, `equity_session_state`, `EQUITY_RTH_OPEN` and
  `EQUITY_STREAM_START` until it is deleted. The technical levels' session
  start reads the 09:30 open from `EQUITY_RTH_OPEN` instead of a literal. No
  behaviour changes.

- **The clock has one home, `sessions.py` (refactor cut C09).** *2026-09-26* —
  `now_et`, `UTC`, `set_runtime_timezone` / `get_runtime_timezone_name` and
  the runtime-timezone globals moved out of `utils.py` into the new
  `intraday_tv_schwab_bot.sessions`. Every reader calls `sessions.now_et()`
  through the module: no module binds the name, and `_strategies.shared` no
  longer re-exports `now_et` (a plugin imports `from ... import sessions`).
  There is no alias. No behaviour changes. The bracket reconcile's
  `account_orders` window (`SchwabExecutor.fetch_order_states`), which read
  the wall clock directly, now reads `sessions.now_et()` too (the same
  instant, still sent as UTC).
  - Tests pin the clock with `tests/support/clock.freeze_et(monkeypatch, at)`
    or the `frozen_et(at)` block, which patch that one attribute, so a pin
    reaches every reader. They replace the per-module `now_et` patches, the
    `_clock` / `_frozen_clock` / `_stage_clock` / `_pin_clock` helpers and the
    conftest loop. A pin must be a trading day unless the test says
    `trading_day=False`.
  - `tests/test_module_layering.py` now rejects any module other than
    `sessions` holding its own reference to the clock, and any test that
    patches or binds `now_et` outside `tests/support/clock.py`.

- **The Schwab client wrapper has its own module, `schwab_api.py`.**
  *2026-09-25* — `call_schwab_client`, `SchwabdevApiUsageTracker`,
  `register_schwab_api_tracker` / `get_schwab_api_tracker` and the
  token-refresh lock moved out of `utils.py`, which no longer imports
  `schwabdev`. There is no alias: import them from
  `intraday_tv_schwab_bot.schwab_api`. `_strategies.shared` no longer
  re-exports `call_schwab_client`. The module also holds the one reading of a
  response, `response_ok`, and `call_schwab_json` (see Fixed).
  - `StartupReconciler` no longer takes `client`; it reads the broker through
    `SchwabExecutor.fetch_account_positions()` and
    `fetch_working_orders(from_ts, to_ts)`. It takes `risk`,
    `book_bracket_cancel_fills` (the manager's, now public),
    `settle_unsettled_entry_orders` and `unsettled_entry_order_ids` (the
    entry gatekeeper's). The engine builds `PositionManager` first.

- **What the shared stage changed, per strategy family.** *2026-09-24*

  **top_tier_adaptive / small_cap_squeeze** (small_cap subclasses top_tier)
  - `_finalize_signal` keeps top_tier's own gates in their order: Fix D,
    stretched / technical bias, ORB 5m follow-through, HTF bias / pivot, HTF
    EMA, and the ORB opposing-level block, now keyed on `regime == "orb"`.
    It then proposes with style = family = the regime.
  - The refusal payload carries every blocker, but top_tier's decision log
    still records one reason per (side, regime) attempt. Only the recorded
    primary can differ from before: the raw R:R gates and the broken-level
    guard now report after top_tier's own gates, the candle veto before the
    exhaustion check, and the ORB opposing block reports its own token
    (`long_orb_opposing_resistance_within_<mult>atr`).
  - The raw R:R gates moved into `admit`: `orb_measured_move_exhausted`, and
    `stop_floor_kills_rr` when sr_scalp's floor widened the stop. The
    sr_scalp token lost `bound_by=` / `pierce_atr=` / `rr=`.
  - The chart veto now reaches top_tier and is on in its preset (a flip,
    below). The dual-divergence and candle vetoes are honoured (off in both
    presets).
  - The ORB structure / S/R bypass is the manifest exemption, keyed on the
    regime rather than the window clock. It is inert in both presets
    (`disable_orb_regime: true`).
  - A refined stop on the wrong side of entry is refused by `admit`
    (`stop_on_wrong_side`) instead of by the gatekeeper.
  - New stamps: `entry_style_family` (the regime) and `regime_rank_unit`.
    `orb_window_entry` is now "family is orb".

  **Peers**
  - `peer_confirmed_key_levels` / `_1m` gate on their LTF (5m; 1m for
    `_1m`), build S/R on the 1m frame, and score FVGs on the 1m frame as
    before (`zone_frame`).
  - key_levels' AND target clearance is now `require_peer_target_clearance`
    (true), no longer riding on `use_sr_filter`.
  - The key_levels ladder is re-qualified at `min_rr` from the refined stop
    and capped at the refined target. New refusal token:
    `no_qualifying_target_rr_after_refine:<rr>`. With both refinements off,
    as in the parity presets, the ladder is unchanged.
  - key_levels builds structure / technical / chart / candle contexts on its
    5m LTF, which it never built before.
  - `peer_confirmed_htf_pivots` / `_trend_continuation` gate on their 5m LTF
    with S/R on the 1m frame. Their structure fields are `msltf_*` (were
    `ms_ltf_*`). htf_pivots' `pivot_rejection` exemption from the structure
    veto is now the manifest's, and it builds its 60m HTF context before
    `admit` for every evaluated side.
  - All four record every blocker of a refusal, shared vetoes included.
  - Stamps: `regime` `key_level` / the pivot family / `trend_continuation`;
    families `peer` / `pivot` / `continuation` (no exit grace).
  - The peers now meet every exit family their YAML switches on; the presets
    keep what the old override ran (see the preset bullet). The ladder
    defence moved to `strategy_exit_signal`.

  **momentum_close, opening_range_breakout, microcap_gap_orb,
  rth_trend_pullback, volatility_squeeze_breakout**
  - Their own blockers and the anti-chase exhaustion checks are one
    proposal's pending reasons. One FVG-retest pass over the union replaces
    the two old passes.
  - Every veto the preset switches on runs; structure and S/R are no longer
    an either/or. The candle veto and the broken-level guard now reach them
    (off in their presets).
  - The chart veto now also meets a retest-admitted entry. It used to run
    only while nothing else was pending, and in the replay 6 rth and 3
    vol_squeeze signals are now refused with `chart_pattern_opposed`.
  - The retest stop anchor is re-clamped by `min_stop_atr_mult`: 20 / 31 /
    31 / 100 / 31 replayed signals got a wider stop, with target, reason and
    score unchanged.
  - `volatility_squeeze_breakout`'s `squeeze_tier_label` /
    `squeeze_effective_target_rr` describe the admitted target, in units of
    the proposal's own risk. A capped target drops to the highest tier it
    still reaches (runner to standard on 85 of 202 replayed signals); this
    supersedes the 2026-05-14 semantics. Its SHORT is not proposed when
    `risk.allow_short` is false.
  - rth_trend_pullback's LONG and SHORT bodies are one side-parametrized
    path.

  **mean_reversion, closing_reversal** (the reference conversions)
  - Their own reasons and every veto are recorded together: the chart filter
    no longer needs "no other reason", and structure / S/R are no longer an
    elif.
  - The dual-divergence veto, the candle veto and the broken-level guard
    reach them (dual off in their parity presets).
  - `final_priority_score` is unchanged.

  **microcap_pm_breakout**
  - One LONG proposal, style `pm_breakout`, family `breakout`, on its 1m
    frame. The decision-1 vetoes judge it: structure, chart, candle and dual
    divergence, none of which reached it before.
  - The 2c/3c candle gate is a non-deferrable pending reason. The decision
    is the same, and the refusal lists every blocker. When it blocks, a
    waiting / rejecting retest plan's own reason is not in the refusal.
  - The PMH stop is the proposal's `stop_resolver`, with the shared retest
    anchor off. The refusal is the plain `stop_above_entry`.
  - It now stamps `final_priority_score` (activity + shared). It still ranks
    on `strategy_priority_score` alone.

  **pairs_residual**
  - Every read is on the traded leg's frame and symbol; the reference is used
    only for the z-score, `reference_symbol` and `pair_id`.
  - Every shared veto reaches it when switched on; the preset keeps only
    structure, as before. The exhaustion checks always run for the ready
    side, and every blocker is recorded.
  - Divergence-only entries are off in the manifest.
  - `final_priority_score` is unchanged.

  **zero_dte_etf_options / zero_dte_etf_long_options**
  - Every style is admitted as a PREMIUM proposal, in the underlying's
    market direction (a bull put credit is LONG), before the chain is read.
    `emit` refuses an option whose `metadata['direction']` disagrees.
  - ZO's LTF structure veto moved from `_regime_confirm` into the stage, per
    style; `midday_credit_spread` is exempt through the manifest. Signals
    are unchanged. When no style fires, the logged reason is now the
    style's own instead of `market_structure_*`.
  - ZO's regime FVG term reads `use_fvg_context` through
    `entry_policy.fvg_regime_scores`. `_attach_option_final_priority_score`,
    which rewrote the signal with `replace`, is gone. Its score is emit's
    `strategy_priority_score` and the rank primary.
  - ZL's ORB path meets `use_structure_filter` / `use_sr_filter` (both on)
    instead of `orb_apply_*_veto`. Its tokens lose the `orb_long_option_`
    prefix, and structure and S/R are recorded together; it is no longer an
    elif. The trend style's own blockers are recorded before the shared
    vetoes.
  - Both loops record every blocker of a style's refusal. Families:
    `option_debit` / `option_credit` / `option_long`.

- **Presets: parity first, then three user-decision flips.** *2026-09-24* —
  with every knob global, a preset that said `true` for a knob its strategy
  never read would have switched that knob on. Every preset (and
  `config.example.yaml` / `config.yaml`) was first rewritten to what its
  strategy EFFECTIVELY ran: a knob it never read is `false`, and a value the
  peer override forced replaces the YAML's.
  - The four peers ship `risk.time_stop_minutes: 0` and their structure /
    chart / candle exits off.
  - The dual-divergence veto is off where it was never read: top_tier,
    small_cap_squeeze, mean_reversion, closing_reversal, pairs_residual, the
    peers and the 0DTE strategies.
  - `microcap_gap_orb`'s candle veto is off; its old `true` was never read.
  - The 0DTE presets ship the technical, HTF-divergence and S/R-proximity
    terms and both refinements off (the FVG term stays on).

  Then three flips, each switching on a veto the strategy never met:
  - `top_tier_adaptive`: `use_opposing_chart_filter: true`. The chart veto
    alone blocked 0 (logged) to 2 (rebuilt) of 162 replayed entries, both
    losers, -2.0R.
  - `peer_confirmed_key_levels`: `use_structure_filter: true`, and only
    that. It blocked 6 of 13 entries on the 5m LTF it reads (2 on 1m, 3 in
    the logged track), and every one with a realized result was a loser.
  - `microcap_pm_breakout`: structure, chart, candle and dual-divergence
    vetoes `true`. Together they removed 4 realized trades worth -3.2R (3
    worth -3.6R after the lookahead correction), all from one session.
    Every replayed entry was at or after 09:58, so the flip is unmeasured
    on the premarket entries its 07:00 window mostly takes; the candle veto
    now abstains on single prints (see Fixed). `use_sr_filter` stays
    `false`: its OR clearance blocked 40-60% of the strategy's entries with
    no edge.

  `small_cap_squeeze` and `peer_confirmed_key_levels_1m` stay at parity.
  `tests/test_preset_parity.py` pins the whole matrix, and
  `configs/README_PRESETS.md` tabulates it.

- **Signal ranking weighs the shared score: top_tier / small_cap_squeeze
  1.0, peers 0.5.** *2026-09-24*
  - One ranker, `SharedEntryPolicy.rank_key`, replaced three: the
    gatekeeper's generic key, top_tier's `signal_priority_key` override and
    the peers' manifest tuple. With weights at 0 it reproduces their orders
    exactly, ties included.
  - The weights are a user decision. top_tier / small_cap_squeeze rank on
    `regime_score_normalized + 1.0 x shared_context_score x
    regime_rank_unit`, where `regime_rank_unit = 1 / (ceiling - floor)` (the
    floor including a SHORT's premium). A shared point therefore moves a
    signal exactly as far as a raw regime point.
  - The peers rank on `ltf_score + 0.5 x shared_context_score`, then their
    tail; htf_pivots' and trend_continuation's side picks use the same key.
  - Until now the entry-context and FVG terms only broke near-ties there.
  - microcap_pm and the 0DTE strategies keep `strategy_priority_score` with
    weight 0 and no `final_priority_score` tiebreak. Everyone else keeps the
    default `final_priority_score`, which already contains the shared terms.

- **The ORB and pullback exit graces are global.** *2026-09-24* — they key
  on the `entry_style_family` the entry stamps, not on `orb_window_entry` /
  `regime == "pullback"`, which only top_tier stamped.
  - `opening_range_breakout` and `microcap_gap_orb` entries now get the ORB
    grace (20 / 25 minutes in their presets).
  - `rth_trend_pullback` entries get the pullback grace: max(10, 15) = 15
    minutes in its preset.
  - A position that was open across the upgrade restart has no
    `entry_style_family` and gets neither grace. It also loses its recorded
    MFE / MAE once. Positions are intraday, so this is a clean break with no
    shim.

- **The broken-level guard is a global knob, and ships off.** *2026-09-24*
  - top_tier's `reject_entry_near_broken_level` is now
    `shared_entry.use_broken_level_guard`, a veto every strategy meets when
    it is switched on. The percent clearance is still scaled by the
    proposal's volatility scale.
  - Its thresholds moved to `shared_entry`: 0.0035 / 0.90 in the top_tier
    preset and 0.0025 / 0.72 in small_cap_squeeze and the code defaults.
  - It is `false` in every preset. It existed only to keep entries clear of
    the S/R-loss exit, which ships off. Over 09-17..09-23 it blocked about
    3.5 setups per session. On a +2R / -1R bracket, the 139 archived blocked
    setups (04-24..09-23) did no worse than real fills (-0.09R against
    -0.04R, difference CI [-0.37, +0.30]); the 57 it alone blocked made
    +0.18R. 110 of 138 closed back through the level within 30 minutes.

- **The divergence age counts session bars, and the HTF divergence knobs are
  global.** *2026-09-24*
  - While session indicators are on and the wall clock is inside the
    session-indicator window (new `utils.indicator_session_open()`, also
    used by `latest_atr14`), the divergence age counts session bars only. A
    premarket reader keeps the all-bar age. Yesterday's closing pivots
    therefore stay live at the open.
  - On symbol-days with a dense overnight tape, the 60m HTF divergence read
    on 0% of the 09:30-11:00 minutes and now reads on 23.6% of RTH minutes
    instead of 10.4% (15m: 16.4% instead of 14.6%). `divergence_max_age_bars`
    (LTF 8) and the HTF age (6) are not retuned.
  - New `technical_levels.htf_divergence_max_age_bars` (6, the old hardcoded
    value). `technical_levels.enabled` / `divergence_enabled` now switch HTF
    divergence off too. The data feed reads all three once in its HTF
    context build, for every strategy and the dashboard, as part of the
    cache key; no strategy passes them.
  - The HTF context cache rebuilds across the session open / close.
  - `levels_shared.find_divergence` takes `bar_clock` as a required keyword.
  - The peers' 60m reads now carry into the late morning. Watch
    `htf_divergence_adjustment` on peer entries in the next dry-run.

- **The scaffold starts a new preset from the code defaults of the shared
  knobs.** *2026-09-24* — `config.example.yaml` runs
  `peer_confirmed_htf_pivots`, so its `shared_entry` / `shared_exit`
  sections, `risk.time_stop_minutes` and
  `support_resistance.entry_proximity_scoring_enabled` are now that
  strategy's parity values. `scripts/scaffold_strategy_plugin.py` still
  clones the example but writes the dataclass defaults for those. The
  scaffolded `strategy.py` proposes / admits / emits and passes the contract
  test. A `--strategy <other>` run against the example still inherits them;
  its header says so.

- **rth_trend_pullback / volatility_squeeze_breakout: the same-level retry
  block stays off.** *2026-09-24*
  - `emit` stamps `entry_price` on every price-level signal. These two never
    stamped it, so `risk.same_level_block_minutes` (30 in their YAML) never
    reached them.
  - For parity their presets now ship `same_level_block_minutes: 0`, and
    every replayed signal gets the same block result as before.
  - Turning it on is a go-live decision for a beta dry-run.

- **The 0DTE presets ship two fixed-but-never-fired blocks off.**
  *2026-09-25* — both were on in the YAML and neither ever fired. Both are
  fixed now (see Fixed), so shipping them on would switch them on for the
  first time:
  - `zero_dte_etf_options` and `zero_dte_etf_long_options`:
    `risk.same_level_block_minutes: 0` (was 30).
  - `zero_dte_etf_options`: `options.credit_pivot_buffer_gate_enabled:
    false` (was `true`). `config.example.yaml` and the dataclass default
    were already `false`.
  - Turning either on is a go-live decision for a beta dry-run.
    `tests/test_preset_parity.py` and `tests/test_zero_dte_shared_entry.py`
    pin the shipped values.

- **top_tier's `vwap_reclaim` is exempt from the S/R veto;
  small_cap_squeeze's is not.** *2026-09-25* — a user decision on what
  was listed under Known issues. `top_tier_adaptive`'s manifest adds
  `vwap_reclaim: [sr]` to `capabilities.shared_entry.exemptions`.
  `small_cap_squeeze`'s manifest keeps the veto on vwap_reclaim. No preset
  value changes: `use_sr_filter` stays on in both.
  - Why: vwap_reclaim enters on the bar that closes back across VWAP after
    a flush, and on a large cap that bar sits right next to an HTF level.
    The veto reads it in one of two ways. It can see a pending level
    (crossed, flip unconfirmed), which always blocks; that rule dates from
    2026-09-23 and has never run live. Or it can find the bar inside the
    0.25% / 0.72 ATR minimum clearance. Nothing is broken: the clearance
    math, the pending rule and the side logic do what they are coded to
    do.
  - Evidence, current code on the study tape: the veto refused all 13
    vwap_reclaim entries of 09-21..09-23 (11 winners, 2 losers, +7.56R).
    With the out-of-sample 09-24 session added, it refused 15 of 16 (11
    winners, 4 losers, +6.74R), which left the regime effectively off.
    The live bot on H: has neither the pending branch nor the session S/R
    ATR, and it traded all 16 for +6.94R.
  - Of the 16, 7 were pending-branch blocks (+3.04R) and 8 were
    nearest-level blocks (+3.71R). The live code makes every nearest-level
    block on the same tape.
  - The earlier "level-code drift" explanation was wrong. The rebuilt
    levels differ from the logged ones because the replay tape is 3-6 days
    deep with no bars before 07:00, while the live REST frame is 10 days
    deep.
  - The veto still helps the other regimes: over 174 archived top_tier
    entries it blocked -14.95R outside vwap_reclaim. So only vwap_reclaim
    is exempt.
  - In small_cap_squeeze it refused only vwap_reclaim losers (3 of 8 in
    June, -2.45R), so that manifest keeps it. Exempting both would have
    pooled +4.29R.
  - Only the veto is skipped. The S/R stop / target refinement, the ladder
    rungs, and the structure and chart vetoes still apply to vwap_reclaim.
    Its signals record `sr` in `shared_entry_gates_exempted`.
  - Options not taken: an `sr_pending` veto token so that only the pending
    branch could be exempted (+3.04R), or taking the pending branch out of
    the veto everywhere (about 0R net).
  - Caveat: 4 sessions and 16 entries, 9 of them on 09-23. Watch
    vwap_reclaim in the next dry-run.
  - Tests: `tests/test_top_tier_megacap.py` checks two LONG reclaims, one
    just back over a pending resistance and one 0.1 ATR under the nearest
    resistance. top_tier admits both, while its pullback control is still
    refused and the structure veto still reaches vwap_reclaim.
    small_cap_squeeze refuses both. Both manifests are pinned.

- **top_tier_adaptive and small_cap_squeeze exempt range / pullback /
  sr_scalp from the structure veto.** *2026-09-25* — a user decision on
  what was listed under Known issues. Both manifests add
  `range: [structure]`, `pullback: [structure]` and
  `sr_scalp: [structure]`. `trend`, `momentum`, `vol_squeeze` and
  `vwap_reclaim` keep the veto, and `orb` keeps `[structure, sr]`. No
  preset value changes: `use_structure_filter` stays on in both.
  - Why: the veto works as coded, but it amounts to "refuse a LONG whenever
    the 5m bias is bearish". Every block was bias-only, and that bias is
    mostly a location reading. `_resolve_structure_bias` checks the
    midpoint of the last swing range before the swing labels, so an HH /
    HL uptrend that pulls back into the lower part of its last swing reads
    bearish.
  - Range and pullback enter against the short-term swing by design. The
    engine already treats them that way: Fix D exempts range and sr_scalp
    as mean reversion, and the structure exit gives pullback its own grace.
  - Evidence: over 162 archived top_tier entries the veto blocked 20 worth
    +7.43R (18 with a result, across 7 sessions). They averaged +0.41R,
    against -0.14R for the entries it kept (CI of the difference [+0.22,
    +0.80]).
  - 12 of the 20 blocks came from the midpoint step, and in 7 of those the
    swing labels agreed with the trade. By regime: range 7 (+2.18R),
    pullback 6 (+4.23R), vol_squeeze 5 (-0.82R), trend 1, and vwap_reclaim
    1 (a rebuild artifact).
  - Effect: blocks go from 20 to 7. That releases 13 entries worth +6.41R,
    but only 8 of them (about +2.1R) are not also refused by the S/R or
    chart veto.
  - Live proxy: about 35 of the 50 post-Fix-D structure refusal episodes
    in H:'s `decisions.csv` are released (32 range, 3 sr_scalp).
  - Caveat: 19 of the 20 blocks predate Fix D (2026-05-27), when the 1m
    structure made the veto nearly inert; the 5m resample made it decisive.
    The post-Fix-D live evidence is a crude path proxy (n=50, CI crossing
    0).
  - small_cap_squeeze mirrors the exemption. It is inert there, because
    the preset runs none of the three regimes.
  - Options not taken: a labels-only veto (shared code, so it changes every
    strategy; blocks 20 -> 13), or a veto only on an active opposing BoS /
    CHoCH (0 of 162 blocks).
  - Tests: `tests/test_top_tier_megacap.py` checks HH / HL labels with a
    bearish bias against a LONG, and the mirror against a SHORT, in both
    strategies. Range, pullback and sr_scalp are admitted with `structure`
    in `shared_entry_gates_exempted`, and the other four regimes are
    refused. The queue fall-through tests and the top_tier /
    small_cap_squeeze scenarios of `tests/test_knob_reach_matrix.py` now
    use a regime the veto still reaches.

### Removed

- **The adaptive ladder's target-exit suppression and zone-flip rung
  promotion.** *2026-09-27* — with the touch hold off (every preset), the
  ladder's first rung is now a plain take-profit on every path.
  - The suppression declined `RiskManager`'s target exit while a strong push
    was under way. Until 2026-05-14 one quote at the target was enough, and
    it did hold targets live: AAPL on 2026-05-13 and TSLA and AVGO on 05-14
    ran past an unchanged target without a target exit. From 2026-05-14 it
    also needed the last closed bar in the frame to have closed through the
    target at least 55% of the way up its range, and a
    `confirmation_indices` ETF still leaning the trade's way.
  - The management frame holds only delivered bars, so that bar had closed
    one to two minutes before the quote being judged, and a quote at the
    target inside it had already been taken as the target. The suppression
    could still act when the quote sampling (one quote per management pass,
    about every 4-25 s) missed a strong 1m close through the target and a
    later quote reached the target before the next bar was delivered (or the
    mark lagged the prints that way). Where it happens the default now takes the target instead of
    holding it for the next bar (the replays below found three, all with a
    quote sampled only once a minute).
  - The zone-flip promotion (stop to the rung's zone edge, target to the
    next rung, a runner past the last) needed two delivered bars wholly past
    the rung's zone while the target still sat on the rung, which only a
    restart past the rung, or an exit order failing for two bars, could
    leave. No ladder adjustment was logged from 2026-05-01 to 2026-09-25.
    There the default now takes the target.
  - Gone: `_ladder_target_strength_confirmed`, `_ladder_indices_still_aligned`,
    the `adaptive_ladder_suppress_target_exit` flag (in
    `RiskManager.update_position` and both ladder metadata builders; a
    restored position that carries it has it ignored), the zone-flip
    promotion, `ladder_last_promoted_price`, and the ladder pass's S/R read
    every cycle (it reads the S/R context only at a touch, with the hold
    on). The `confirmation_indices` stamp stays: ENTRY_CONTEXT and
    EXIT_CONTEXT log it.
  - Replayed before and after, the touch hold off, on the live frame shape:
    27 settings (the bar path, the points per leg, bar delivery at 0 / 10 /
    50 s, with and without the shared exits, quotes sampled every tick or
    every 6 s (two phases), 15 s, 30 s or 60 s (two phases), and the price's
    time lagging 6 s), over the 105 archived laddered trades, the 8 recorded
    target exits and the 23 fixture-tape entries: 1,746 trade-walks per tree
    with the same exit time, price, reason and R in all but the case above,
    which appears only with quotes sampled once a minute and depends on the
    sampling phase: AMD 2026-09-24 (its 12:26 bar closed strongly through
    the target between two samples; the old code held the target at the
    next two sampled quotes and took it two minutes later, 0.15R lower) and,
    in a verifier's wider grid (every tick, 3, 6, 10, 15, 20, 30, 45, 60 at
    six phases and 90 s), ADBE (the new default 0.56R better) and NVDA
    (0.28R worse: there the hold paid). None appears with a quote every 30 s
    or more often. A crafted tape shows the case
    (`tests/test_ladder_touch_hold.py`).
  - Tests: the suppress-flag, passed-rung, promotion-price, ladder index
    re-check and posture tests went with the code (`test_bug_regressions.py`,
    `test_index_symbols.py`, `test_bar_posture.py`,
    `test_fix_strategy_consumers.py`, `test_silent_excepts.py`,
    `test_properties.py` g1-g3); `tests/test_ladder_touch_hold.py` pins the
    rung-1 take-profit, a crafted missed-sampling case included.

- **Dead code (refactor cut C08).** *2026-09-25* — none of these had a caller, or each
  duplicated what it inherits:
  - `utils.opposite_side` and `utils.talib_bbands` (and its `_strategies.shared` re-export);
  - `candles.CUSTOM_2C_PATTERNS` and `daily_stats.EMPTY_STATS`;
  - `levels_shared.session_dates` (only a test called it);
  - `BaseStrategy._effective_relative_volume` / `_relative_volume_gate_threshold` (the screeners resolve to `screener_base`'s);
  - the 0DTE strategy's `_option_quote_force_cooldown_seconds` and its `insufficient_bars_reason` classmethod (the module-level function is the one called);
  - `microcap_pm_breakout`'s screener `watchlist_mode`, a byte-identical copy of the one it inherits from `microcap_gap_orb` (which now answers the same calls);
  - `SchwabExecutor._EQUITY_WORKING_STATUSES`, `submit` and `submit_raw`.

- **Breaking: `runtime.timezone` is retired; the bot's clock is always New York
  (refactor cut C10).** *2026-09-26* — only the prior-day/week bucketing
  was pinned to ET. Another `runtime.timezone` value made the clock and the
  bar timestamps read that zone's wall time, so the session gates, the
  stream window and bucket grid, the ORB opening range and every configured
  HH:MM time (entry, management and screener windows, blackouts, time-decay
  knobs) moved by the zone offset. `sessions.EXCHANGE_TZ` is now the one zone;
  `set_runtime_timezone`, `get_runtime_timezone_name`,
  `models.DEFAULT_RUNTIME_TZ`, `levels_shared._SESSION_TZ` and the ORB's
  `_ET_ZONE` are gone, and `load_config` no longer writes a process-wide
  zone. A config that still sets `runtime.timezone` (any value,
  `America/New_York` included) fails at load with
  `retired config keys -- runtime.timezone: ...`. Every shipped preset
  dropped the line. **A local `configs/config.yaml` must drop it too before
  the bot restarts on this version**, or the bot will not start; a config
  that used another zone must also convert every configured time to ET.

- **Breaking: renamed and retired config keys fail at load.** *2026-09-24* —
  a stale YAML fails at load with the replacement named, rather than
  silently doing nothing.
  - `shared_entry.use_divergence_filter` is now `use_dual_divergence_veto`.
    It was always the hard RSI+OBV veto; the LTF counter-divergence
    penalties its name and docs claimed belong to
    `use_technical_entry_adjustment`.
  - `shared_entry.use_htf_divergence_filter` is now
    `use_htf_divergence_score`; it is a score term and never blocked.
  - `technical_levels.divergence_block_dual_counter` is removed; the veto is
    `use_dual_divergence_veto` alone.
  - Strategy params, checked against `strategies.<name>.params`
    (`config._RETIRED_STRATEGY_PARAMS`):
    - top_tier_adaptive / small_cap_squeeze: `orb_bypass_structure_entry`
      and `orb_bypass_sr_entry` (now the manifest exemption),
      `reject_entry_near_broken_level`, and
      `broken_level_min_clearance_pct` / `_atr` (now `shared_entry.*`);
    - peer_confirmed_htf_pivots / _trend_continuation: `use_sr_veto` (now
      `shared_entry.use_sr_filter`);
    - zero_dte_etf_long_options: `orb_apply_structure_veto` /
      `orb_apply_sr_veto` (now the shared structure / S/R vetoes).
  - `test_param_declaration_drift` also forbids a manifest from declaring a
    retired param.

- **Breaking: the strategy plugin API.** *2026-09-24*
  - `strategy_logic_default`, `signal_priority_key`, `position_exit_signal`
    and `shared_exit_signal` are reserved; defining one raises `TypeError`
    at import. Strategy exits go in `strategy_exit_signal`, and ranking goes
    in the manifest.
  - The shared-entry helpers left `BaseStrategy` for the policy: the veto
    predicates and reasons, the refinement passes, the retest plans and
    stop anchor, the score terms, the divergence candidate,
    `_build_signal_metadata`, `_build_bullish_reversal_signal`,
    `_target_meets_min_rr`, `_clamp_refined_stop`, `_shared_entry_enabled`,
    `_shared_entry_value`. A strategy builds signals only through
    `entry_policy.emit`.
  - Signal-metadata and token changes:
    - HP / TC structure fields are `msltf_*`;
    - sr_scalp's `stop_floor_kills_rr` token lost `bound_by` / `pierce_atr`
      / `rr`;
    - microcap_pm's `stop_above_entry` has no values;
    - ZL's ORB tokens lost the `orb_long_option_` prefix.
  - `execution.close_position` takes a required `qty`, and
    `levels_shared.find_divergence` a required `bar_clock`.

### Fixed

- **One stream thread: a pass inside schwabdev's reconnect window no longer
  starts a second stream, a stop there stops it, and a failed subscription
  send no longer fails the pass.** *2026-10-06* — schwabdev 4.0.0's
  `Stream.active` turns False when a reconnect's backoff ends and True again
  only at the new connection's LOGIN response, while its thread runs all the
  while: through the streamer-info fetch (25 s on 10-02, 11:12:38-11:13:03),
  the connect and the login.
  - `start_streaming` started the stream whenever `active` was False, and
    `Stream.start` refuses only while `active`: a pass in the window ran a
    second thread and connection over the first one's websocket and event
    loop. None is archived (on 10-02 the engine was blocked in a REST call
    through the window). It now starts one only when none runs
    (`MarketDataStore._stream_running`): `active` is False and the thread of
    the store's last start is not alive. That is schwabdev's private
    `Stream._thread`, read right after `Stream.start` (pinned against
    schwabdev 4.0.0 by a test); the store keeps the reference itself because
    `Stream.stop` waits 5 s for the thread and then drops its own, while a
    thread sleeping out a backoff (up to 120 s) lives on, and a new start
    would wake it into the new stream's loop: no stream starts until it
    ends. A pass that finds the stream reconnecting logs that at DEBUG.
    Each such pass also re-stamped the start (`stream_start_requested_at`)
    and cleared the first-bar bookkeeping; now only a start does, so
    `stream_connect_timeout_seconds` counts from the start and a stream slow
    to come up falls back to `price_history` as that setting says (the
    re-stamp held the fallback off unless a pass outlasted the timeout). The
    clearing also made the first bar after a reconnect fetch the bars the
    outage cost, but only when a pass happened to land in the window;
    without it a reconnect's missing bars stay missing, as they did on 10-02
    (every symbol's 11:11 bar, to the day's end). Fetching them after every
    reconnect's login is queued.
  - `stop_streaming` stopped the stream only while `active`: in the window
    it did nothing, and schwabdev's thread reconnected with the recorded
    subscriptions and kept merging bars into `live`. It now stops it
    whenever it is active or its thread runs, and logs a WARNING when the
    thread outlives the stop (schwabdev's 5 s join). With nothing running
    (every idle pass) it does nothing and logs nothing, as before.
  - A stream send's failure raised out of the engine's pass, ahead of
    management: schwabdev builds each request from its streamer info, which a
    failed reconnect leaves None (10-02 11:13:03), and its `basic_request`
    then fetches the info on the engine's thread (a blocking REST call) and
    raises `ConnectionError("Streamer info unavailable")` when that fails
    too. `start_streaming` now catches any error building or sending the
    CHART_EQUITY change, keeps the subscription as it was (`stream_symbols`,
    so the same change goes out next time), and logs it with its type, at
    WARNING for the first failure of a run (`Schwab stream send failed
    (ConnectionError: Streamer info unavailable): CHART_EQUITY ADD of 2; ...`),
    DEBUG for the rest and INFO for the send that ends the run. After a
    failure no stream send is tried for 60 s (`STREAM_SEND_RETRY_SECONDS`),
    nor while the stream is not active (schwabdev's reconnect fetches the
    info on its own thread): `MarketDataStore._stream_send_due`, which the
    LEVELONE_EQUITIES subscription is to share.
  - The `stream_fields` comment in `config.example.yaml` and the top_tier
    preset gave another order (1=ts ... 7=seq, 8=chart_time); it now states
    schwabdev's corrected one, which the bar parser reads: 0=symbol,
    1=sequence, 2=open, 3=high, 4=low, 5=close, 6=volume, 7=chart_time,
    8=chart_day. The other presets carry no such comment.
  - Identical: the 10-01 09:40-10:40 stepped replay (top_tier, a page open)
    and the peer_confirmed_key_levels 05-04 09:40-10:40 window, against
    2d70ab2, in every category (the harness's stream is active throughout
    both).
  - README: `stream_fields` and `stream_connect_timeout_seconds`.
  - Tests: `tests/market_data/test_stream_lifecycle.py` (new): a start only
    when no stream thread runs (active; inactive with its thread alive, the
    start's bookkeeping kept; a thread that ended or was dropped; a stopped
    thread still running holds the next start off until it ends); a stop
    while the thread reconnects, of an active stream, and with nothing
    running (quiet); a failed send never raising, logged with its type, the
    next held 60 s and while inactive, a run's WARNING then DEBUG then INFO, a
    failing `send`, and an UNSUBS failing after its ADD went out. Through
    schwabdev's own connection loop on the fake streamer (no network): a pin
    of 4.0.0's `Stream._thread` and `active` through the window (its `start`
    runs a second thread there; its `stop` drops the reference); a store's
    start in the window running no second thread; its stop there ending the
    thread with nothing replayed; and 10-02's failed streamer-info fetch,
    the send's error caught with no second blocking fetch until the stream is
    back and 60 s have passed. `tests/market_data/test_stream_receiver.py`
    (the parser reads schwabdev's CHART_EQUITY order),
    `tests/guards/test_preset_parity.py` (every `stream_fields` comment states
    it), `tests/support/brokers.py` (`_FakeStream` keeps schwabdev's
    `_thread`; `_FakeStreamer` holds a stream thread's fetch). 31 mutants, all
    killed, each by its named test.

- **A malformed stream message no longer reconnects the stream: the
  CHART_EQUITY receiver never raises into schwabdev, and a malformed bar is
  dropped instead of merged.** *2026-10-06* — schwabdev 4.0.0 reads an
  exception out of its receiver as a broken connection (`stream.py:133-136`):
  it logs `Stream unknown exception`, tears the websocket down and reconnects
  after its backoff, and every symbol's bars stop until the new connection
  replays the subscriptions. `MarketDataStore.on_stream_message` raised on
  any malformed CHART_EQUITY message: one that is not a JSON object, a `data`
  packet or item that is not one, `content` that is not a list, a chart time
  that is not epoch milliseconds. Two kinds of chart time raised nothing and
  were merged into the 1m frame: ±2**63 ms, which parse to NaT (the row sorts
  last, and every indicator read of the symbol's frame then raises on it),
  and one past the message's receipt, which stays the frame's last bar from
  then on (every later bar sorts before it, and the symbol's latest bar never
  ages). None
  of H:'s 72 logs (2026-05-01 to 10-02) shows a receiver exception: their one
  `Stream unknown exception` (10-02 11:13:03) is schwabdev's own, on a failed
  streamer-info fetch.
  - Each item is now parsed on its own (`MarketDataStore._merge_chart_equity`,
    new): a malformed one is dropped with a WARNING naming the error's type
    (`Dropped a malformed CHART_EQUITY item (ValueError: ...): {...}`, the
    item cut to 400 characters, `STREAM_LOG_TEXT_CHARS`), and the message's
    other items and packets merge as they would without it. A message, `data`,
    packet or `content` of the wrong type is dropped with a WARNING naming the
    type it was. A chart time that parses to NaT, or that is more than 60 s
    after the message's receipt (`STREAM_BAR_MAX_LEAD_SECONDS`: a bar is
    stamped with its minute's start, so a real one is before its receipt; the
    minute allows for a local clock behind Schwab's), is malformed. Anything
    else that raises (a defect, not the data) is caught in
    `on_stream_message` and logged at ERROR with its type; the stream stays
    connected. The DEBUG line of a payload that is not JSON names the error's
    type too.
  - Unchanged: a well-formed message's bars and frames, the stale-candle
    WARNING, an item with no symbol or chart time or whose symbol is not an
    equity ticker (skipped without a word), and a field that is not a finite
    number (read as 0.0).
  - Identical: the 10-01 09:40-10:40 stepped replay (top_tier, a page open)
    and the peer_confirmed_key_levels 05-04 09:40-10:40 window, against
    2d70ab2, in every category.
  - Tests: `tests/market_data/test_stream_receiver.py` (new): 27 malformed
    shapes, each dropped with one WARNING naming it and nothing merged; a
    malformed item or packet beside well-formed ones, which merge exactly as
    without it; the silent skips as before; the 60 s bound at 60 and 61 s; an
    injected merge defect caught at ERROR with its type, the store's lock
    freed; the cut of a long item; the non-JSON DEBUG line. Through
    schwabdev's own connection loop on a fake streamer (no network): a pin
    that a receiver that raises makes schwabdev reconnect, and every
    malformed shape in turn leaving the one connection up and the next
    well-formed bar merged. `tests/support/brokers.py` gains `_FakeStreamer`
    (schwabdev's websocket and streamer-info fetch, scripted). 22 mutants,
    all killed, each by its named test.

- **A symbol whose step frame or history-fetch decision cannot be built no
  longer fails the cycle.** *2026-09-28* — after the pooled frame map, the
  step read every watchlist symbol's frame again (`bars.setdefault(symbol,
  get_merged(...))`, whose default is built before the lookup). For a
  symbol whose build had raised in the pool (logged, and named in
  PRECOMPUTE_FAILURES), that read raised again outside any isolation and
  failed `step()` before position management: no position was managed that
  cycle (stops included), no entry was taken, and while it lasted the
  loop's sleep doubled up to 60 s and the engine escalated. The history-fetch
  decisions ahead of the fetch (`WarmupTracker.should_fetch_symbol_history`
  and the lookback, per watchlist symbol) were a loop with no isolation
  either, with the same effect. With `cycle_precompute_workers: 1` the
  pool's serial branch had no isolation at all, for the S/R and context
  pre-warms too. No archived day logged a PRECOMPUTE_FAILURES event
  (2026-05-01 to 09-25).
  - Now each symbol of the CPU maps is isolated (`_compute_symbol_map`):
    the error is logged at WARNING with its type (`Merged frame precompute
    failed for XYZ (consecutive=1): ValueError: ...`, `History fetch
    decision failed for XYZ (consecutive=1): ...`), the symbol is left out
    of the map's result, one PRECOMPUTE_FAILURES event names the map's
    failures, and the other symbols run as usual. A symbol without a frame
    gets no pre-warm, no entry and no frame-based exit that cycle; a
    position in it is still managed on its quote (stop, target, force
    flatten). A symbol whose history decision failed is not fetched that
    cycle. The fetch pool's failures are logged the same way, with the type.
  - The traceback is throttled per map and symbol
    (`IntradayBot._symbol_map_failed`, 2026-09-29, the final review's
    probe): on the first failure of a run and every
    `SYMBOL_MAP_TRACEBACK_EVERY`-th (10th) after, a one-line WARNING in
    between; the run ends when the symbol builds again in that map.
    As first cut, a symbol that failed every cycle logged its full
    traceback every cycle in each map it failed in (44 lines a cycle for
    one symbol, some 30-50k an hour), where the cycle failure it replaces
    was throttled so.
  - The second read is gone. It copied every symbol's frame each step and
    threw the copy away, and for a symbol a stream bar reached between the
    map and the read it rebuilt the whole frame with its indicators and
    threw that away.
  - Tests: `tests/composition/test_cycle_symbol_maps.py` (with the
    throttle: tracebacks on the 1st, 10th, 20th and, after a success, the
    next 1st).

- **The daily history holds completed sessions only.** *2026-09-28* —
  during the session Schwab's daily `price_history` ends with today's
  forming bar (every archived fetch of 2026-09-18..25 counted it), and
  `MarketDataStore.get_daily_history` cached it for the day as it stood at
  the fetch. top_tier's 20-session ADR (its `vol_scale`) and 60-session
  sector beta therefore took five minutes of the session at the 09:35 entry
  pass, fifteen at 09:45 (09-21, 09-22) and three hours after the 12:35
  restart (09-23). On the archived days that partial bar's true range was a
  median 0.62 of a full session's (0.28-1.33, p10-p90), so it moved the ADR
  by a median -1.9% (-3.6% to +1.6%), and `vol_scale` and every threshold it
  scales with it (a decision change on top_tier and small_cap_squeeze). The
  bars dated today (ET) are left out now, so the frame no longer depends on
  when it was fetched, which the prewarm fetch needs (above): before the
  open there is no bar for today. A response with only today's bar is no
  history (a WARNING, cached for the day). The trim rests on Schwab
  stamping a daily candle at its day's midnight Central, which the replays'
  synthetic daily bars cannot check. The prewarm's 09:15 fetch cannot check
  it either: before the open there is no bar for today, so its `Daily
  history for X: N sessions` line shows N one lower than the archived days'
  in-session 124-125 whether the trim works or not. Only a fetch made after
  the open does: on the first dry-run day, restart the bot once after 09:30
  (or read the line of a symbol that joins the watchlist mid-session) and
  check that its N equals the same symbol's 09:15 N. One higher means the
  forming bar was kept, and the stamp is not what the trim assumes.

- **A filled exit's slippage reaches its record, for every exit on a level,
  signed; and a stop moved while a scale-out slice works is logged.**
  *2026-09-28* — `exit_slippage` was stamped for `stop` and `target` exits
  only, as `abs(fill - level)`, and into the position metadata after the
  exit's EXIT_CONTEXT had been built, where no record read it: the
  structured snapshot does not carry it, so none of the 33 archived days
  has it. The peak giveback, whose floor is the level a third of the
  top_tier exits turn on (study B), had none.
  - A filled EXIT_CONTEXT (the engine's, and one the broker filled) now
    carries `exit_slippage`, measured from `exit_level`: the fill's
    distance past it per unit, positive when worse for the position (a LONG
    sold below it, a SHORT covered above it), negative when better; and
    `exit_slippage_r`, that over the trade's initial risk. The profit
    lock, trail and break-even exits are `stop` exits on the level they
    set (`stop_source` names it); the peak giveback's is its floor; a
    bracket child's, or the static disaster stop's, is the price it rested
    at.
  - None for an exit on no level (a time stop, a shared or strategy exit,
    force flatten, the touch hold's close and timeout verdicts), nor for a
    fill whose price is estimated. The position metadata no longer carries
    it.
  - While a scale-out slice works at the broker, the pass ends at the
    working-exit check, whose risk check on the shares outside the slice
    (`_working_slice_remainder_exit`) can still move the stop. That move
    reached no record: the pass returned before the POSITION_ADJUSTMENT
    records, and the next pass reset `management_adjustments` first. The
    list is now reset ahead of the check and the pass logs what it moved
    (`PositionManager._log_position_adjustments`, the one writer of the
    record), with the pass before. The stop move itself applied before as
    now; live only (a dry-run exit never works).
  - Tests: `tests/runtime/test_management_instrumentation.py`
    (`TestTheExitLevelAndSlippage`, `TestExitLevel`,
    `TestAWorkingSlicesRatchet`).

- **A startup refusal is logged to the day's log, `start_trading_bot.bat`
  keeps its window open after a failed run, and an error that ends the run
  no longer skips the shutdown cleanup.** *2026-09-28* — `cli.main` let a
  config the loader refused raise out as a traceback on stderr. The log is
  set up once the config has loaded (`IntradayBot.__init__`), so no log
  recorded the refusal, and the `.bat`'s window, double-clicked, closed on it
  at once: a config error could not be read. A bot that could not be built
  from a loaded config (a strategy param or `blackout_file` refused when the
  strategy is built) and an error that ended the run also reached no log.
  - Each is now logged at CRITICAL, with the error's type, to the console
    and the day's log (`Startup refused: the config configs/config.yaml
    could not be loaded: ValueError: ...`, `Startup refused: the bot could
    not be built: ...`, `The bot stopped on an error: ...`), its traceback at
    DEBUG to the log only, and the bot exits with status 1, as before. A
    refused config has no `runtime.log_dir` to read: its refusal goes to the
    default, `.logs/bot_<ET date>.log` under the working directory (every
    preset's; the start scripts `cd` to the checkout). A stop signal while
    the config loads is not a refusal and is not caught.
  - `IntradayBot.run` runs `_shutdown_cleanup` in a `finally`: an exception
    escaping the start-up (the start-up reconcile) or the loop outside a
    cycle (the daily archive, the housekeeping) skipped it, so the
    dashboard and the stream were never stopped and no session report was
    written. The cleanup now runs once, with stop signals held as on a stop,
    and the exception still escapes `run`, so the process exits nonzero and
    `Restart=on-failure` restarts the bot.
  - `start_trading_bot.bat` ends `if %ERRORLEVEL% neq 0 pause`: after a run
    that exits nonzero the window waits for a key, and a clean stop closes it
    as before. `neq 0` also catches a crash's negative NTSTATUS exit, which
    `if errorlevel 1` misses. Its header says so, and that a scheduled start
    (Task Scheduler) should run `.venv\Scripts\python.exe main.py --config
    configs\config.yaml` itself, with the task's "Start in" set to the
    checkout: every path in that line, and the `.logs` folder, is relative
    to it (the `.bat`'s `cd` set it), and an empty "Start in" runs the task
    in `C:\Windows\System32`, where none is found (2026-09-29).
    `start_trading_bot.sh` is unchanged: it runs in a terminal, which stays
    open.
  - README.md ("Running") and README_LINUX_DEPLOY.md (a failed or
    restarting unit) say where the refusal is.
  - Tests: `tests/composition/test_startup_refusal.py` (new; one runs
    `python main.py` on a refused config from another directory and reads
    the day's log), `tests/composition/test_start_scripts.py`,
    `tests/composition/test_engine_shutdown.py` (`TestACrashIsNotAStop`: one
    cleanup before the crash escapes, and a signal during it is held).

- **A broker bracket's `STOP_LIMIT` limit sits `bracket_stop_limit_offset_r`
  x the position's initial R past its trigger, wherever the stop has moved.**
  *2026-09-28* — the offset is documented in units of initial R, but every
  caller of `SchwabExecutor.bracket_stop_limit_price` measured R from the
  entry to the stop being placed: the entry, a partial fill's resize, the
  `replace` sync, a re-protect after a partial exit, the runner's stop-only
  re-protect, a late-fill resize and a restore. Once break-even moved the stop
  to the entry (+0.02R) the offset was 0.01R, and 0 at partial break-even: the
  limit sat on the trigger, so the first print through the stop left it
  unfilled (study B 3.3 #1: a median 0.025R on the archive's 35 moved-stop
  exits).
  - `bracket_stop_limit_price(side, stop_price, *, initial_risk)` takes the
    per-share initial R, and so, keyword-only, do `_bracket_exit_children`,
    `build_protective_oco_order`, `submit_protective_oco`,
    `ensure_position_protected`, `resize_bracket_children` and
    `sync_bracket_levels`; their `entry_price` parameter is gone. The callers
    pass `position_metrics.initial_risk_per_unit(position)` (|entry -
    `metadata['initial_stop_price']`|, never the current stop), the TRIGGER
    entry its limit's distance to the stop (no fill yet), the fill its
    distance to the post-fill stop, an adopted entry the same.
  - A `STOP_LIMIT` child with no positive initial R is refused with a
    `ValueError` rather than priced at a made-up offset (a `STOP` needs none).
    A `restore_basic` position now carries `initial_stop_price`, the stop it is
    restored with, as every entered position does; its R-based reads (the
    trail's activation, the peak give-back) no longer re-base on a moved stop.
    A restored row saved without one (only a `restore_basic` row from before
    this change) gets no `STOP_LIMIT` protection: the restore logs the error
    with its type and the engine owns the exits.
- **The engine checks its own stop every cycle, whatever rests at the
  broker.** *2026-09-28* — `TradeManager.update_position` stood the stop
  exit down whenever a live bracket tracked a stop child. Two states left the
  position with no working stop: a `STOP_LIMIT` that triggered with the price
  through its limit, which stays a working order with nothing filled (study B
  3.3 #2), and a replace that failed, which left the child at an older level
  (3.3 #3). No order state the bot reads tells a triggered stop from a
  resting one (the bot's working statuses include both `WORKING` and
  `AWAITING_STOP_CONDITION`; which Schwab shows for each was never verified),
  and a partial fill shows only on some. The one sign the engine sees every
  cycle is its mark at or through the stop, and that is exactly when the
  deferral applied, so it is gone rather than qualified.
  - A stop exit goes out as every engine exit does (`_manage_position`): the
    bracket is cancelled first, what its children filled before the cancel
    landed is booked as `broker_stop` at the broker's price, and only the rest
    is sold; a cancel that cannot be confirmed defers the exit. A stop that
    fills as the cancel arrives is booked, and nothing else is sent.
  - A resting target child still owns the target exit (the stop rests beside
    it). While a scale-out slice works beside a remainder stop, a stop hit on
    the shares outside the slice now cancels the slice, and the full exit
    follows once it settles.
  - Dry runs are unchanged: their bracket is simulated, and the engine always
    owned every exit.
- **A bracket records the levels its working children actually rest at.**
  *2026-09-28* — `bracket['stop_price']` and `['target_price']` were written
  only by the bot: the entry, an adoption, a replace the broker acknowledged.
  The fill reconcile now reads them off each cycle's order state
  (`PositionManager._track_resting_levels`, for a child still working whose
  level reads as a finite number) and logs a WARNING naming both levels when
  the broker holds another. The `replace` sync then compares the engine's
  level with what rests and puts it back, and a fill reported without a price
  is booked at it. A failed replace's WARNING names the stop the engine now
  enforces.
- **An entry that fills through its levels is protected at the post-fill
  fallback levels.** *2026-09-28* — the entry gatekeeper books such a fill
  with `trade_management.default_levels`, but the bracket kept the signal's:
  the TRIGGER's children rested at its stop, on the wrong side of the fill,
  until a `replace` sync moved them a cycle later (never, in `static` mode),
  and protection placed afresh when no child materialised went in there too
  (study B 3.3 #4).
  - `submit_equity_entry` takes `post_fill_levels`, required for a bracketed
    entry (a `ValueError` without it): the gatekeeper's `_post_fill_levels`
    rule, the signal's levels or the fallback with the reason they no longer
    fit, which also books the position. `_finalize_bracket_protection` places
    fresh protection at the post-fill levels, and replaces resting children
    onto them after any resize, in either sync mode. A replace that fails
    leaves the bracket recording the signal's level, with an ERROR, and the
    engine enforces the fallback stop itself.
  - An entry adopted from its order's fill record after its submit returned
    moves the children it adopts the same way. A dry run's simulated bracket
    records the post-fill levels. A child the fill already triggered cannot be
    moved; the reconcile books its fill.
  - Tests: `tests/runtime/test_bracket_defects.py` (21, marked `regression`)
    runs the real `SchwabExecutor` in live mode against a fake Schwab order
    book: the offset at every call site; a triggered, unfilled `STOP_LIMIT`
    taken over (cancel first, then the exit), with its partial fill booked
    first, and one that fills as the cancel arrives; the engine's stop above
    a stale broker level; the tracked levels; the fallback on resting,
    fresh, adopted and dry-run protection. Each of 18 mutants re-introducing
    a defect fails at least one. `test_bracket_orders.py`,
    `test_execution_invariants.py`, `test_sweep_fixes.py`,
    `test_partial_exit.py`, `test_asset_type.py` and `test_broker_payloads.py`
    call the new signatures; the old stop-suppression test now pins the
    engine's stop.

- **A network error or a body that is not a JSON object on a 0DTE
  option-chain read no longer fails the engine's cycle.** *2026-09-28* —
  both 0DTE strategies read the chain through `call_schwab_json`, which lets
  a transport failure through unchanged and returns whatever JSON the body
  decodes to, and `_fetch_raw_option_chain`
  (`zero_dte_etf_options/chain.py`) caught only `SchwabHTTPError`. A refused
  or dropped connection, a timeout, a broken body or a redirect loop raised
  out of the read, and a 2xx body that was not a JSON object (a list, null)
  raised out of `parse_option_chain`. Raised by the build path's read,
  either left `entry_signals` and failed `step()` after position management,
  on every cycle in which a candidate whose chain read failed reached a
  build (a style fired on it): the cycle's other candidates got no entry,
  the end-of-cycle marks were skipped, the dashboard showed the error
  instead of the cycle, and consecutive failed cycles doubled the loop's
  sleep, up to 60 s. The spreads' prefetch logged and swallowed the error
  but recorded no failure, so the build path read the chain again and
  raised.
  - `schwab_api.SCHWAB_TRANSPORT_ERRORS` names what the Schwab client raises
    for a transport failure: requests' `ConnectionError` (with
    `ConnectTimeout` and `SSLError`), `ChunkedEncodingError`,
    `ContentDecodingError` and `TooManyRedirects` (a redirect loop: the
    session follows 30 redirects). It was checked against schwabdev 4.0.0
    with loopback servers failing each way: a timed-out read ends as a
    `ConnectionError`, never `ReadTimeout`, and the token refresh swallows
    its own network errors. The errors requests raises for a request it
    cannot build (`InvalidURL`, `MissingSchema` and the like) are not in it,
    nor those of a redirect to a URL it cannot use (`ValueError`,
    `InvalidSchema`), which propagate like a bug.
  - The chain read takes a transport failure like an error response: a
    WARNING under `..._strategies.zero_dte_etf_options.chain`, the failure
    remembered (`_option_chain_read_failed_at`, so the chain is re-read
    after `option_chain_cache_seconds`), and the build's
    `option_chain_unavailable`. The warning names the error's type now:
    `Option chain read failed for SPY: ConnectionError: ...`, and
    `SchwabHTTPError: ...` for an error response.
  - It takes a 2xx body that is not a JSON object the same way, checked
    before `parse_option_chain` runs: `Option chain read failed for SPY:
    list body, not a JSON object: []`, the body's repr cut at 120
    characters.
  - Anything else the read raises propagates from it, and nothing is
    remembered: a bug, such as schwabdev's parameter validator's
    `TypeError`, a request requests cannot build (`InvalidURL`), or a JSON
    object that `parse_option_chain` cannot read. Raised from the build
    path's read, it fails the cycle as before, on a cycle in which a style
    fires on that candidate.
  - The spreads' prefetch is a warm-up and never fails the cycle. A chain
    the read found unavailable is remembered, so the build path does not
    read it again. Anything else a read raises in the prefetch is logged
    with its type and traceback (`Option chain prefetch failed for SPY:
    TypeError: ...`), at WARNING at most once a minute per symbol and at
    DEBUG in between (`log_setup.ComponentFailureLog`: the prefetch runs
    every entry cycle), and the other symbols still warm; the build path's
    own read decides for a candidate that needs the chain.
  - `log_setup.ComponentFailureLog` (the dashboard's, the exit record's and
    now the prefetch's failure log) read a component that had never warned
    as warned at 0.0 on the monotonic clock, so a first failure within a
    minute of the host's boot logged at DEBUG only; it now always warns.
  - `requests` is a direct dependency (`requirements.txt`, `pyproject.toml`)
    at 2.34.2, the version `constraints.txt` pinned. The layering guard lets
    `schwab_api` import it.
  - Tests: `tests/test_zero_dte_chain.py`. `TestAFailedRead`: each transport
    error type and a list, null or string body, on the read and on the
    prefetch; a `TypeError`, an `AttributeError`, requests' `InvalidURL` and
    a JSON object the parser cannot read, which propagate from the read; a
    2xx `{}`, an empty chain; one the parser cannot read in the prefetch,
    logged while the other symbols warm; a bug in the prefetch, logged with
    its traceback while the other symbols warm, once a minute per symbol,
    and raised again by the build path's read. `TestTheCycleGoesOn`: an
    entry cycle of each strategy over SPY and QQQ with SPY's chain failing:
    QQQ enters when SPY's chain is unavailable, a bug raises when a style
    fires on SPY, and QQQ enters when none does, a body the parser cannot
    read included. The C44 warnings test reads both warnings with the
    error's type. `tests/test_schwab_api.py`: `call_schwab_json` passes each
    transport error through unchanged, a request requests cannot build is
    not one, and a 2xx body that is not an object comes back as it is.
    `tests/test_log_setup.py`: a first failure in the host's first minute
    warns.

- **A stop signal between cycles shuts the bot down cleanly.** *2026-09-26* —
  the engine routed SIGTERM through KeyboardInterrupt, but only the session
  reconcile and `step()` sat inside the loop's `except KeyboardInterrupt`. A
  `systemctl stop`, `kill` or Ctrl+C that landed anywhere else raised out of
  `run()` with a traceback and skipped the shutdown: the dashboard and the
  stream were not stopped, no session report or archive was written, and the
  process's closed trades never reached `trades.csv`. "Anywhere else" means
  the inter-cycle sleep, where the loop spends most of its time, the
  archive / rollover / prune housekeeping, the auto-exit check, the error
  path's gate and publish, and start-up (the dashboard start and the
  start-up reconcile).
  - `run()` now catches the interrupt around start-up and the whole loop and
    runs `_shutdown_cleanup` once. The auto-exit returns to it instead of
    calling it itself. `run()` then logs `Shutdown complete.` and returns, so
    the process exits 0. An exception that escapes `run()` is still a crash:
    it propagates without the cleanup, as before, so `Restart=on-failure`
    restarts the bot.
  - While `run()` runs, SIGINT and SIGTERM (and SIGHUP and Windows'
    SIGBREAK, see the next two entries) share one handler, and the
    previous handlers are restored when it returns or raises. The first
    signal raises. Any later one is only recorded, and logged once the
    cleanup is done. A second Ctrl+C or SIGTERM used to raise inside the
    cleanup and leave it half-done.
  - `DashboardServer.start` keeps its server only once the serving thread
    runs. Before, `stop()` after a start interrupted earlier than that waited
    forever in `shutdown()`.
  - `README_LINUX_DEPLOY.md` says what the shutdown writes (the reconcile
    metadata is not among it: it is saved as positions change), when its two
    log lines appear, and how to spot a shutdown that `TimeoutStopSec` cut
    short.
  - Tests: `tests/test_engine_shutdown.py` (every stop point, a crash that
    must still escape, real in-process signals, and a real SIGTERM sent to a
    child process in its sleep) and `tests/test_dashboard.py`
    (`TestStopAfterAnInterruptedStart`).

- **A terminal hangup shuts the bot down cleanly.** *2026-09-26* — SIGHUP
  was not among the stop signals `run()` takes, so an SSH disconnect or a
  killed tmux pane with the bot in the foreground ended it on the spot: no
  dashboard or stream stop, no session report, archive or `trades.csv`
  append. SIGHUP now shares the SIGINT / SIGTERM handler while `run()` runs
  (one during the cleanup is ignored and logged, like theirs), and its
  previous handler is restored afterwards. A bot started with SIGHUP ignored
  keeps it ignored: under `nohup` a hangup still leaves the bot running, and
  `kill <pid>` stops it cleanly. Windows has no SIGHUP. After a real hangup
  the process exits 120 rather than 0 (Python cannot flush stdout to the
  terminal that is gone); the report, archive and log file are written as on
  any stop. `README_LINUX_DEPLOY.md`'s tmux and `nohup` notes say so. Tests:
  `tests/test_engine_shutdown.py` (in-process hangup and `nohup` cases, a
  real SIGHUP sent to a child process, a real `nohup`, and a child whose
  pseudo-terminal is closed under it).

- **Ctrl+Break shuts the bot down cleanly on Windows.** *2026-09-26* —
  SIGBREAK was not among the stop signals `run()` takes, so Ctrl+Break in
  the bot's console window ended it on the spot, without the cleanup.
  SIGBREAK now shares the SIGINT / SIGTERM / SIGHUP handler while `run()`
  runs, taken like SIGHUP: where the platform has it (Windows) and it is not
  ignored. One during the cleanup is ignored and logged, and its previous
  handler is restored afterwards. On Windows only Ctrl+C wakes Python's
  `time.sleep`, so a Ctrl+Break in the sleep between cycles acts when that
  sleep ends (`runtime.loop_sleep_seconds`, 2 s by default, while the stream
  runs; `idle_sleep_seconds`, 60 s, outside it). Closing the console window
  is not covered: it arrives as SIGBREAK too, but Windows ends the process
  as soon as the C runtime's handler returns, before the cleanup can finish.
  `start_trading_bot.bat`'s header says both. Not run on Windows: the tests
  in `tests/test_engine_shutdown.py` stand SIGUSR1 in for SIGBREAK where the
  signal module has none.

- **The start scripts pass their arguments on to `main.py`.** *2026-09-26* —
  `start_trading_bot.sh` and `start_trading_bot.bat` ran
  `python main.py --config configs/config.yaml` and dropped whatever they
  were given. The `--env /path/to/custom.env` their headers offered for
  multi-instance setups never reached the bot, which ran on the
  auto-discovered `.env`, and neither did `--strategy` or `--config`. Both
  now append their arguments (`"$@"`, `%*`) after the default `--config`. A
  `--config` of your own replaces it, since argparse keeps the last one.
  The headers list the flags and note that relative paths resolve from the
  repo folder the scripts `cd` into. With no arguments, both start the bot
  exactly as before. The `.sh` header's setup step now creates the
  virtualenv with `python3.11 -m venv .venv`: the bot needs Python 3.11+,
  and a plain `python3` can be older (3.9 on Debian 11). Tests:
  `tests/test_start_scripts.py`.

- **`kill <pid>` of `start_trading_bot.sh` stops the bot cleanly.**
  *2026-09-26* — the script ran `python` as a child of bash, so a SIGTERM
  sent to the script's PID killed bash and left the bot running, orphaned
  and without its shutdown. The launch line is now
  `exec python main.py ...`: the bot takes over the script's PID (its
  command line is then `python main.py ...`). `start_trading_bot.bat`
  cannot do the same, since cmd.exe has no `exec`; its header now says to
  stop the bot with Ctrl+C in its window, and that ending the `cmd.exe`
  alone leaves the bot running. Tests: `tests/test_start_scripts.py`.

- **A regular `pip install .` reports the right version.** *2026-09-26* —
  `intraday_tv_schwab_bot.__version__` read `version.txt` from the package's
  parent directory: the repo root in a checkout, but `site-packages/` in an
  installed wheel, which does not ship the file. A regular install reported
  `0.0.0` while the wheel's metadata said 1.0.0, and the reader turned an
  empty file or any error reading it into `0.0.0` as well. The version file
  is now `intraday_tv_schwab_bot/VERSION`: package data the wheel and sdist
  ship, the file `pyproject.toml`'s dynamic version reads at build time, and
  the file `__version__` reads beside `__init__.py`. A checkout run with
  `python main.py`, an editable install and a regular install report the
  same version, and a missing or unreadable file fails the import instead of
  reading `0.0.0`. Releases bump `intraday_tv_schwab_bot/VERSION`, in
  normalized PEP 440 form. Tests: `tests/test_packaging.py`.

- **An sdist built in a working checkout no longer ships the tests.**
  *2026-09-26* — setuptools' default sdist rules take `tests/test*.py`, so
  an sdist built in the dev tree carried the untracked test modules (96 of
  them, without the conftest and helpers they need). A new `MANIFEST.in`
  prunes `tests/`, and keeps `configs/config.yaml`, any `.env` (at the root
  or beside the config), `.schwabdev/` and `.logs/` out whatever else
  brings them in, an earlier build's egg-info `SOURCES.txt` (which
  setuptools reads back) included. Its lines only take out and must stay
  last; each warns at build time while it has nothing to take out. The
  wheel is unchanged by it: the package and its metadata (the next entry
  keeps the dev-tree plugins out of both). Tests:
  `tests/test_packaging.py` (offline sdist and wheel builds of a copy of the
  package among seeded dev-only and private files).

- **No build carries the two microcap plugins that live only in the dev
  tree.** *2026-09-26* — `microcap_gap_orb` and `microcap_pm_breakout` are
  kept out of git, but a build run in the dev tree took them: the sdist and
  the wheel (`python -m build`, and `pip install .`, which builds the wheel
  straight from the tree) carried their modules (`packages.find` includes
  `intraday_tv_schwab_bot*`), their `manifest.json` and `README.md` (the
  `package-data` globs), and whatever an earlier build's egg-info
  `SOURCES.txt` listed, which setuptools reads back. `pyproject.toml` now
  excludes both packages from `packages.find` and their files from the
  package data (`exclude-package-data`), and `MANIFEST.in` prunes both
  directories, which also keeps a stale `SOURCES.txt` from bringing them
  back. Each piece is needed: without the exclude, a wheel built in the tree
  still ships their modules; without `exclude-package-data`, their manifests,
  which an installed bot would list as plugins whose modules are missing;
  and without the prune lines, everything a stale `SOURCES.txt` lists. Built
  from a copy of the dev tree, the wheel and the sdist each lose those 10
  files and nothing else. The checkout still loads them: `python main.py`
  imports the package from the tree, and so does an editable install
  (setuptools' default mode maps the whole package directory). Like the
  other `MANIFEST.in` lines, the prune lines print a notice at build time
  while they have nothing to take out, in a clean clone for one. Tests:
  `tests/test_packaging.py`.

- **The deploy guide's recovery command finds the bot.** *2026-09-26* —
  for a terminal wedged by the first OAuth, `README_LINUX_DEPLOY.md` gave
  `kill -9 $(pgrep -f intraday_tv_schwab_bot)`. The bot's command line is
  `python main.py --config ...` (or the venv's `python` by path), and the
  package name is not on it, so the command matched nothing and killed
  nothing. The guide now lists the bots with
  `pgrep -af '^[^ ]*python[^ ]* main\.py --config'` and kills the wedged one
  by PID. The pattern finds the bot however the guide starts it (by hand,
  the systemd unit, `start_trading_bot.sh`, the tmux one-liner). Anchored at
  the interpreter, it leaves out the tmux server, which keeps the one-liner
  on its own command line, and an `sh -c` wrapper. A
  `kill -9 $(pgrep ...)` one-liner would also have killed those, and a bot
  the systemd unit runs. That was the guide's only `pgrep`, `pkill` or `ps`
  line. Tests: the new `tests/test_deploy_guide.py` (each way of starting
  the bot, run for real with a stand-in `main.py`, against the real
  `pgrep`).

- **An unquoted YAML time is read everywhere.** *2026-09-26* — YAML reads an
  unquoted `10:05` as the sexagesimal integer 605 (and `9:30` as 570; a
  zero-padded `09:30` stays a string). `parse_hhmm` and `is_time_in_window`
  accept that integer, but the readers below `str()`-ed the value first, and
  `"605"` does not parse. They now pass the raw value:
  - a macro blackout's `start` / `end`, inline or from `blackout_file`: on
    the event's date every entry-block and force-flatten check raised
    instead of blocking;
  - top_tier / small_cap_squeeze `orb_end_time`: with the ORB regime on, the
    strategy refused to construct ("cannot parse the ORB window") and the
    ORB-window check raised. The signal metadata recorded `"605"`; it now
    records `HH:MM`;
  - top_tier / small_cap_squeeze `early_session_stop_widening_until`: an
    `except Exception: pass` swallowed the parse error, so the early-session
    stop widening never applied. The guard is gone: a malformed value no
    longer switches the widening off; it stops the bot at startup (see
    Changed);
  - zero_dte_etf_long_options `orb_start_time` / `orb_end_time` /
    `orb_opening_window_start` / `orb_opening_window_end`: pandas read
    `between_time("570", "574")` as an empty opening range without raising,
    and the ORB-window check raised;
  - microcap_pm_breakout `pm_reference_window_start` / `_end`: every entry
    cycle raised.

  `options.force_flatten_time` already read the integer, through a
  hand-written normalizer. The normalizer is gone and the raw value reaches
  `parse_hhmm`, as the other option times do. **Behaviour change:** a blank
  (`null` or `""`) value is no longer replaced by 15:18; load refuses it,
  naming the key (see Changed). No shipped preset or generated file is
  affected: every shipped time is quoted, and `scripts/sync_macro_events.py`
  writes strings. The README now says an unquoted time is accepted. Tests:
  `tests/test_top_tier_megacap.py` (`TestEventBlackouts`),
  `tests/test_orb_regime.py` (`TestAnUnquotedOrbEndTime`),
  `tests/test_top_tier_adaptive_new_regimes.py`
  (`TestVolatilityWideningFactor`), `tests/test_zero_dte_shared_entry.py`
  (`TestTheLongOptionLoop`), `tests/test_bug_regressions.py`
  (`TestMicrocapPmPmhPriorDayHigh2026_04_28`) and
  `tests/test_config_validation.py` (`TestOptionsValidation`).

- **small_cap_squeeze's empty `index_symbols` no longer reads as SPY / QQQ.**
  *2026-09-26* — the preset and its manifest ship `index_symbols: []` for
  "no index confirmation", but top_tier's per-symbol lookup
  (`_indices_for_symbol`) turned an empty list into `['SPY', 'QQQ']`, which
  `active_watchlist` never streamed. The entry gate never asked
  (`require_index_confirmation: false`), but every signal was stamped with
  that list as `confirmation_indices`. The adaptive ladder's re-check
  (`_ladder_indices_still_aligned`) found no SPY / QQQ bars in the feed and
  read that as a turned tape, so small_cap_squeeze never held a target exit
  for a rung's zone flip. It also fetched SPY's daily history once a day to
  stamp a `sector_beta` against it. `index_symbols` is now read in one place
  (`_index_symbols`) by the watchlist and the lookup, and an empty list or a
  missing key means no index ETF. **Behaviour change (small_cap_squeeze
  only):** signals stamp `confirmation_indices: []` and no `sector_beta` /
  `sector_beta_benchmark`, and the SPY daily fetch is gone. The ladder's
  index re-check is now inert: a target tag that comes after a bar has
  closed through the rung, at least 55% of the way up its range (down, for
  a SHORT), with the rung's zone not yet flipped, now waits for the flip
  instead of exiting at the rung. Entries do not change. On the recorded
  AAPL / TSLA / SPY sessions (2026-04-15/16, 17 trades) no decision changed:
  both target tags came on the first bar through the target, where the
  closed-bar check fails either way. top_tier_adaptive (`[SMH, IGV, XLK]`)
  and every preset value are unchanged. Tests: `tests/test_index_symbols.py`.

- **Gate attribution reads the peer family's `long.` / `short.` blockers.**
  *2026-09-26* — the peer strategies (key_levels and its _1m twin,
  htf_pivots, trend_continuation) list every blocker of a side they refused
  under that side's prefix (`long.market_structure_bearish(...)`,
  `short.missing_htf_pivot`). The session report's gate attribution read a
  side only from `long_` / `short_` and `build_failed_long_` /
  `build_failed_short_`, so a peer row's first blocker was scored in the
  direction of the candidate's screener bias, and when the candidate had no
  bias the row was dropped: always on key_levels, whose screener sets none,
  and on htf_pivots / trend_continuation when the screener had no lean. The
  blockers after the first were never scored. `reasons.reason_side` now
  reads all three spellings (the dot one through `split_side_prefix`, see
  Changed); `session_report._reason_side` is gone. **Behaviour change:**
  only the `gate_attribution` block of a peer preset's `manifest.json`.
  Every `long.` / `short.` blocker on a skipped row is now scored in its own
  side's direction, so those gates appear (under the gate's name, with the
  side in `sides`: see the report's gate key below), and a gate scored on
  the wrong screener side can change direction. No other strategy writes
  the prefix. Nothing at runtime reads the block. Tests:
  `tests/test_gate_attribution.py` (a real htf_pivots refusal of both sides)
  and `tests/test_reasons.py` (`TestReasonSide`, with the old reader as an
  oracle).

- **The session report tallies each skip under one gate, and scores the gate
  that refused a signal.** *2026-09-26* — three defects in how the report
  bucketed skip reasons:
  - A skip reason's detail after a `:` split its bucket. The EOD filter
    rejections and gate attribution cut a reason only at its first `(`, so
    key_levels' `long_level_score_below_min:2.50<2.90` and
    `long.htf_bias_not_bullish:neutral(2v1)` and the entry stage's
    `order_failed:<message>` made a bucket per score, vote count or broker
    message (12 buckets for one gate on a synthetic 40-minute key_levels
    session), and the peer family's `long.x` / `short.x` were two gates
    where the entry cycle summary counted one. `reasons.reason_gate`, the
    cycle summary's key (no `long.` / `short.` side prefix, cut at the first
    `(` or `:`), is now the key of all three tallies; a side spelled into
    the name (`long_no_fresh_breakout`) stays part of it. The filter
    rejections keep the raw reasons, sides included, under `variants`; each
    gate attribution entry carries `sides`, its blocks per side, and a row
    on which one gate stopped both sides counts a block on each.
  - A signal a strategy other than top_tier built and an engine gate then
    refused (`max_positions`, `correlation_concentration`,
    `order_failed:...`) was scored under the signal's own name, and the gate
    never was: the report recognised a built signal by top_tier's names
    alone. The entry stage now marks every decision about a built signal
    with the way it bets on the symbol (`market_side`, in the `Decision` log
    line and in `decisions.csv` after `side_pref`), and gate attribution
    scores the gate after the signal's reason on that side, for every
    strategy. For an option that is the underlying's direction, not the
    order side (`RiskManager.market_side`, read out of `same_level_anchor`,
    which reads it as before): a refused 0DTE bear put used to be scored as
    a LONG.
  - rth_trend_pullback's `rth_trend_pullback_long` / `_short` ended like
    top_tier's `top_tier_pullback_long`, so regime-call outcomes counted each
    of its signals, entered or refused, as a top_tier `pullback` call. The
    matcher now reads top_tier's own `top_tier_<regime>_<side>`, whole.

  **Behaviour change:** reporting only; no trade, preset or feed input
  changes. The `Decision` line and `decisions.csv` gain `market_side`. The
  EOD filter rejections (the log table and `SESSION_REPORT`'s
  `filter_rejections`) and the manifest's `gate_attribution` merge the
  buckets above: the peer family's colon details (key_levels,
  key_levels_1m, htf_pivots, trend_continuation), `order_failed:` /
  `order_unsettled:` on every strategy, and a refused signal whose reason
  carries a `:` (rth_trend_pullback, momentum_close, opening_range_breakout,
  mean_reversion, closing_reversal); the peer family's gates lose their
  `long.` / `short.` prefix (the side is in `sides`). A refused signal of
  any strategy but top_tier / small_cap_squeeze now scores its engine gate
  instead of its own name; top_tier's are scored on the same side as
  before. `regime_call_outcomes` loses rth_trend_pullback's calls, and
  `ENTRY_CYCLE_SUMMARY` is unchanged (until the entry below that drops a
  refused signal's own reason from it). A log written before the change (an
  upgrade mid-session) carries no `market_side`, so in that day's archive
  its refused signals, top_tier's included, read as skip reasons. Tests:
  `tests/test_report_skip_buckets.py` (a refused signal through the entry
  stage, the day's log and the session archive; the three tallies on the
  bot's own reasons), `tests/test_gate_attribution.py`,
  `tests/test_regime_call_outcomes.py`, `tests/test_reasons.py`
  (`TestReasonGate`) and `tests/test_option_same_level_block.py`
  (`test_market_side`).

- **Option numbers no longer let a NaN or an infinity through an `a or b`
  fallback.** *2026-09-26* — `a or b` on a number keeps a NaN (it is truthy),
  so the fallback it was written for never ran, and a bare `float()` let a
  NaN or ±inf through the same way. Every such read on the option chain,
  quote, leg, dry-run fill, option entry and paper-account paths now goes
  through `numeric` with `finite=True`. A number reads exactly as before, 0
  included (it still falls through where it did); only a missing, NaN,
  infinite or unparseable value reads differently, and so does a zero sent as
  a string: `a or b` kept any non-empty string, so `"0"` and `"n/a"` read 0,
  and at the `a or b` sites below (strike, mark, total volume, a stored
  strike, the dry-run order `qty`) they now fall through to the second
  source as a numeric 0 does.
  - `parse_option_chain`: a NaN `strikePrice` read 0.0 instead of the
    chain's strike key, which put a leg at strike 0 and made the spread
    width the whole strike; a NaN `mark` read 0.0 instead of `last`, and a
    NaN `totalVolume` 0 instead of `volume`. ±inf now falls through the same
    way, and an infinite bid, ask or greek reads 0.0 / None, so no
    `OptionContract` holds a non-finite number. `contract_from_quote` does
    the same for a stored `strike` (then `strikePrice`) and for an infinite
    quote field.
  - `realized_max_loss_per_contract`: a NaN strike width read as a realized
    max loss of 0.0, so the overage warning could never fire; an infinite
    fill read a credit spread's as 0.0. Both now return None and the check
    is skipped, as for a missing width. `net_price_frac_of_width`: a NaN
    price came back as a NaN fraction, which passed
    `max_net_price_frac_of_width`; it is now None, which the gate rejects.
  - Option entries: a NaN `max_loss_per_contract` raised `ValueError` in the
    sizing floor and ended the entry cycle; the signal is now skipped as
    `option_qty_zero`. A NaN `entry_price` passed every level check and the
    order went out; it now fails `invalid_entry_or_stop`. A NaN or infinite
    fill price booked the position at NaN (stop 0.01, target 0.02) or ±inf;
    it now books at the previewed entry, as a missing fill price did. A NaN
    or infinite strike width no longer writes a NaN / inf max loss or max
    profit.
  - Dry-run option fills: an `inf` limit price filled on the first attempt
    at inf, and a NaN one read as not filled; both are now
    `dry_run_missing_limit_price`. A NaN or infinite leg or order quantity
    raised; the leg quantity now falls back to the order's `qty`, then 1,
    and an order `qty` of `"0"` or `"n/a"` falls back to 1 as a numeric 0
    did.
  - Paper account option rows: an infinite `max_loss_per_contract` made the
    row's and the account's max risk inf, and a NaN max profit or breakeven
    went into the row as NaN. They now read as missing (the max reward then
    comes from the premium target, as a single option's always has).
  - No shipped preset or feed is known to produce these values: quotes come
    from the normalized (finite) quote cache and every metadata field is
    written from `OptionContract` numbers. The raw Schwab option chain is
    the one broker input read directly. Tests:
    `tests/test_options_nan_fallbacks.py` (103, including two properties that
    every finite chain and stored-strike reading equals the pre-fix one).

- **A NaN or infinite order, fill, broker-position or level number no longer
  slips through the runtime.** *2026-09-26* — a NaN fails every comparison,
  so a bare `float()` that let one through skipped the check it fed without
  a word, and an infinity raised or went into the books as it was. These
  reads now go through `numeric` with `finite=True`. A finite value reads
  exactly as before; only a missing, NaN, infinite or unparseable value
  reads differently.
  - Entry levels: `EntryGatekeeper._entry_levels_valid` passed a NaN entry,
    stop or target. An equity signal with a NaN stop then raised
    `ValueError` out of the sizing floor and ended the entry cycle; one with
    a NaN target was entered as a runner, and an infinite target was kept.
    They now fail `invalid_entry_or_stop` / `invalid_target`, as an
    unparseable level already did. `RiskManager.floor_discrete_units` sizes
    no units on a NaN or infinite budget or unit cost, where `math.floor`
    raised.
  - Fills: an exit fill that is not a finite number books at the mark, then
    at entry, flagged estimated, as a missing one did. A NaN fill booked NaN
    P&L into the paper account and the risk manager's `realized_pnl`, after
    which the daily-loss check never fired again that session and every
    risk-state save failed (the column is `NOT NULL`). An equity entry fill
    that is not a finite number books at the previewed limit, as a missing
    one did; a NaN one booked the position at NaN and the account's cash
    went NaN. An option entry left working records such a fill as missing
    too; an infinite one priced its late fills at 0.0001 or inf.
  - Broker order state (`SchwabExecutor`): an infinite order `quantity` on a
    vertical made every leg ratio 0 and raised `ZeroDivisionError` out of
    the order-state read, and with it the bracket fill reconcile. An
    infinite leg quantity read the fill as 0 and the net price as inf, an
    infinite execution quantity read the average fill price as NaN, and an
    infinite `price` / `filledPrice` / `averagePrice` / `stopPrice` was kept
    (an adopted stop took an infinite `stopPrice` over the bracket's own).
    Each now reads as missing, as a NaN one already did.
  - Broker positions: a `longQuantity` / `shortQuantity` that is not a
    finite number (NaN, an infinity, an unparseable or whitespace-only
    string; an absent or empty one is still 0) read as 0 held, so the settle
    booked a tracked position `closed_outside_bot` at the last mark, dropped
    it and cancelled its broker stop. `broker_position_side_qty` now reports
    such a row as unread, and its unused average-price element is gone. The
    settle leaves the position tracked and fails the attempt, so it is
    retried (see the next entry). An
    ignore-list symbol with such a row is now blocked, as when the account
    read fails; it read as not held. A restore fails the attempt, naming the
    row, on such a quantity (it raised a bare `ValueError` /
    `OverflowError`) or on an `averagePrice` that is not a finite number (a
    NaN one restored the position at an entry of 0.01), once it has
    restored every other row (see below).
  - Paper account rows: a NaN or infinite `initial_stop_price` /
    `initial_target_price` went into the dashboard row as it was. It now
    reads as missing, and the max reward falls back to the live target.
  - **Behaviour change:** only on non-finite or unparseable values, which no
    shipped preset or feed is known to produce. Quotes come from the
    normalized (finite) quote cache, levels from strategy arithmetic on
    finite bars, and Schwab sends JSON numbers, though Python's JSON parser
    does read a bare `NaN` / `Infinity`. Two of the changes also cover an
    unparseable or whitespace-only broker quantity (`"n/a"`), not only NaN /
    ±inf: the settle keeps the position tracked instead of booking it
    `closed_outside_bot`, and an ignore-list symbol with such a row is
    blocked where it read as not held (a 2026-09-25 rule). README: the
    reconcile section says how an unread broker row is handled. Tests:
    `tests/test_runtime_nan_reads.py` (129, including properties that finite
    levels, sizing and broker rows read as before) and
    `tests/test_startup_reconciler.py` (`TestIgnoredOpenPositionParsing`).

- **The reconcile fails an attempt on a broker row it cannot use, and the
  session report counts a refused signal under its gate alone.**
  *2026-09-26* — four gaps the 2026-09-26 NaN-read and skip-bucket fixes
  left:
  - A restore read a broker row with no `averagePrice`, or one of 0 or
    below, as an entry of 0.01, as it did a NaN one. `restore_basic`'s
    levels around 0.01 are ones the live price is already past, so the
    first management cycle exited the restored position: a LONG at its
    target, a SHORT at its stop. Such a row now fails the attempt, naming
    it (`the broker position row for AAPL holds no usable averagePrice
    (None)`), as a NaN or infinite one does, once every other row is
    restored (see the next entry); the restore modes block entries
    (`startup_reconcile_failed`) until a retry reads a usable one.
  - The settle left a tracked position whose broker row's quantity cannot
    be read (NaN, ±inf, `"n/a"`) tracked, but still reported the attempt
    settled, so in `block` and `log_only` nothing read the row again until
    the next day. The attempt now fails, and the engine retries it on the
    usual backoff (60 seconds, doubling to 5 minutes); the position is not
    held, so it is still managed meanwhile. Which entries are blocked is
    unchanged in practice: in `block` the held row already blocks them
    (`broker_positions_present`), `log_only` never does, and a restore mode
    already failed on the same row. A vertical whose legs are out of step
    still leaves the attempt settled: its rows were read, and a retry would
    read them the same.
  - A signal the strategy built and an engine gate then refused was counted
    as a skip twice, under the gate and under its own reason. The entry
    stage now counts only the gate after a decision `market_side` marks: in
    `session_skip_counts` (the EOD filter rejections and the manifest's raw
    counts) and in `ENTRY_CYCLE_SUMMARY`, whose entry-decision payload
    carries `market_side` for it. On a synthetic 40-minute rth_trend_pullback
    session `rth_trend_pullback_long` (51 in the filter rejections) is gone,
    and the total drops from 212 to 161.
  - Gate attribution split a refused signal's row at every comma outside
    parentheses, so a broker message holding one
    (`order_failed:live_unfilled_cancel_failed:cancel_error:{'error': ...,
    'error_description': ...}`) scored a second, spurious gate
    (`'error_description'`). Everything after the signal's reason is now the
    one gate the entry stage wrote.

  **Behaviour change:** the restore modes fail the attempt, and block
  entries, on a row with an absent, zero or negative `averagePrice` they
  used to restore (and exit at once); a tracked position's unread row fails
  the attempt in every mode, which adds retries (and, for a tracked
  ignore-list symbol with nothing else held, blocks every entry in the
  blocking modes until the row reads). No preset changes, and no feed input
  is known to send either row. Reporting: `session_skip_counts`, the filter
  rejections and `ENTRY_CYCLE_SUMMARY`'s `top_skip_reasons` lose each
  refused signal's own reason, of every strategy (`top_tier_*`,
  `peer_confirmed_*`, `rth_trend_pullback_*`, the 0DTE `{style}_bull` /
  `_bear`, pairs' `relative_*_z=...`); gate attribution loses the spurious
  gates. The dashboard's entry-decision payload gains `market_side`.
  README: the reconcile section's unread-row bullet and the filter
  rejection and gate attribution bullets. Tests:
  `tests/test_startup_reconciler.py` (`TestAnUnreadBrokerRow`, and the
  engine's retry cadence in `TestEngineReconcileRetry`),
  `tests/test_report_skip_buckets.py`, `tests/test_entry_cycle_summary.py`,
  `tests/test_gate_attribution.py`, `tests/test_runtime_nan_reads.py` and
  `tests/test_sweep_fixes.py`.

- **A restore restores every broker row it can use before it fails the
  attempt on one it cannot.** *2026-09-26* — a restore failed the attempt
  at the first broker row whose quantity or `averagePrice` it could not
  use (see the entries above), so every row after it in the account's
  order was left unrestored, its position unmanaged by the bot, until a
  retry could read that row: for a row the broker kept sending that way,
  all session. The restore now leaves each such row, restores every other
  one, and then fails the attempt naming each row it left (`the broker
  position row for AAPL holds no usable averagePrice (None); the broker
  position row for NVDA holds an unreadable quantity (...)`, after `the
  broker working-order read failed` when an unread order list lets the
  restore run without bracket mode). As for any failed attempt, entries
  are blocked (`startup_reconcile_failed`) and the engine retries on the
  usual backoff (60 seconds, doubling to 5 minutes). The restored
  positions are managed meanwhile, stops included, and the retry restores
  a left row once it reads and nothing twice (a tracked symbol is skipped,
  so no second stop is placed for it). `restore_hybrid` prunes no saved
  metadata while a row is left, so the row still restores from its saved
  levels; the first attempt that reads every row prunes as before.

  **Behaviour change:** only in `restore_basic` / `restore_hybrid`, and
  only beside a broker row the restore cannot use, which no feed is known
  to send: the rows after it are restored and managed at once instead of
  after the retry that reads it, and the error names every such row, not
  the first. The attempt still fails, and blocks entries, until that row
  restores. No preset changes. README: the reconcile section's unread-row
  bullet. Tests: `tests/test_startup_reconciler.py`
  (`TestARestoreBesideAnUnusableRow`, and `TestEngineReconcileRetry`) and
  `tests/test_runtime_nan_reads.py`.

- **An unreadable restore universe no longer restores every broker
  position.** *2026-09-26* — `StartupReconciler._is_restore_eligible_symbol`
  read the strategy's `restore_eligible_symbols()` inside a silent
  `except Exception`. An error there read as "no universe", so every broker
  equity position was restored and managed, including its stop and exits,
  whatever the strategy's universe. The error now fails the restore
  attempt: nothing is restored, entries are blocked
  (`startup_reconcile_failed`, with the error in the message) and the
  attempt is retried. The hook cannot fail on any shipped
  manifest or params. Tests: `tests/test_startup_reconciler.py`.

- **A stored position whose metadata does not parse is no longer restored
  as if from its metadata.** *2026-09-26* —
  `ReconcileMetadataStore._deserialize` read an unparseable
  `metadata_json` as `{}` inside a silent `except Exception`, and the row
  still matched its broker position. It was restored as `restore_hybrid`
  (`restored_from_metadata: true`) with none of its metadata (initial stop,
  ladder state, bracket and working-exit order ids), even under a strategy
  that refuses a restore without it. The row now fails, and
  `load_positions` logs it (`Skipping startup reconcile metadata row due to
  parse error`) and skips it. The position is then restored like one with
  no stored row: `restore_basic`, or skipped with a WARNING where the
  strategy requires the metadata. The bot writes the column with
  `json.dumps`, so only a damaged or hand-edited database reaches this.
  Tests: `tests/test_position_store.py`.

- **A failed read no longer lets an entry through, and a failed error report
  no longer stops the bot.** *2026-09-26* — several entry gates read their
  input inside a `try` whose `except` produced the value the gate passes on,
  so an error let the entry through; and the engine's error path could
  itself raise out of `run()`.
  - After a failed cycle the engine re-evaluates the gate and republishes the
    dashboard state. That can fail the way the cycle did (a dashboard
    snapshot read that raises every cycle), and the exception escaped
    `run()` and stopped the bot on an error the path only meant to report.
    It is now the dashboard's failure, logged and shown as in `step()` (see
    "A failed dashboard update no longer fails the cycle" below), and the
    loop and its backoff go on. A stop signal still reaches the shutdown.
  - top_tier's ORB 5m follow-through gate (`orb_require_5m_followthrough`):
    an error building the 5m frame set it to None, which skipped the gate.
    The entry is now refused as `<side>_orb_5m_unavailable(error=...)` and
    the error logged. (The shipped presets run `disable_orb_regime: true`,
    so the gate does not run there.)
  - top_tier's confirmation bar (`require_entry_confirmation_bar`) took a bar
    whose read raised as a confirmation, and a missing open as 0. An
    unreadable bar now confirms nothing.
  - top_tier's vol_squeeze volume gate: a box volume median that raised or
    was NaN made the baseline 1 share, which any bar cleared. It now fails
    the gate.
  - top_tier's pullback maturity checks, relative-strength gate and
    `require_htf_ema_alignment` skip when their input is missing, and an
    error reading it was taken for "missing". Those reads now raise out of
    the cycle, which the engine logs: `_default_htf_context_for_score`, like
    `_htf_context` and the other context builds, no longer turns a build
    error into None.
  - `get_daily_history` treats a payload whose candles do not read as a
    failed fetch (logged, cached for the day). Its error used to escape
    uncached, so top_tier refetched it every cycle and read it as "no
    stats".
  - The shared stage's `_target_meets_min_rr` (the raw R:R gate, the target
    caps, the divergence target) passed a non-numeric level, and a NaN one
    with `min_target_rr` off. A level that does not read as a finite number
    now fails.
  - The correlation concentration guard logged an unreadable strategy config
    and let the signal through without the guard. It now refuses it as
    `correlation_guard_unavailable`.
  - 0DTE: `live_activity_score` returned the neutral 1.0, which
    `min_activity_for_entry` passes, for a frame whose read raised, and the
    VIX gates read a volatility quote whose freshness check had raised. Both
    errors now propagate.
  - No shipped preset or data-feed input changes: on data that reads, every
    gate decides as before. `_strategies/README.md` (HTF context for the
    score terms) says that a build that raises propagates.
  - Tests: `tests/test_fail_closed_gates.py` (new),
    `tests/test_engine_shutdown.py` (`TestTheErrorPathKeepsTheLoop`),
    `tests/test_orb_regime.py` (`TestTheFollowThroughGateFailsClosed`),
    `tests/test_shared_entry_policy.py` and
    `tests/test_risk_state_persistence.py`.

- **A failed dashboard update no longer fails the cycle.** *2026-09-26* —
  the engine builds the dashboard state at the end of every cycle, after
  the cycle's management, exits and entries. An error building it (a symbol
  snapshot or S/R row read that raises; several such reads no longer pass
  silently, see "67 silent broad excepts" under Changed) escaped `step()`
  and failed the cycle, and the error path's own update then failed the
  same way and stopped the bot (see the entry above).
  - The update is now the dashboard's alone. `IntradayBot._publish_state`
    catches a failure of the build, in `step()` and on the error path
    alike; the cycle counts as done and the loop keeps its normal cadence.
    The backoff and ENGINE DEGRADED stay for the cycle's own errors.
  - It is logged as `Dashboard update failed (consecutive=N)`: a WARNING
    with the traceback on the first failure in a row and every 30th (about
    once a minute at the 2 s cycle), a DEBUG line in between, and
    `Dashboard update recovered after N failed update(s)` once one succeeds.
  - The dashboard (the page and `state_path`) keeps its last state, with
    the status `stale` (`error` while the cycles themselves fail) and a
    message that adds the error and the time of the last failure to the
    cycle's own; `Updated` stays the time of that state. The status chip
    shows `stale` in the error colour (`DashboardServer.publish_stale`).
  - A stop signal during the update still reaches the shutdown.
  - `_publish_state` evaluates the gate itself when the caller has none (the
    error path) and loses its `context_refresh_active` argument, which both
    callers took from the gate. The state it builds is unchanged: every
    shipped preset publishes a running state on a stubbed cycle.

  **Behaviour change:** a dashboard fault no longer slows management or
  raises ENGINE DEGRADED, and the page says it is stale instead of showing
  its last good state as live. README: `runtime.error_escalation_cycles`
  and the `dashboard` section. Tests: `tests/test_dashboard_update_failure.py`
  (new: a real bot's cycle with a broken snapshot or S/R row read, the
  loop's cadence and escalation, recovery, the error path, the log
  throttle, the gate's refresh flag and stop signals),
  `tests/test_engine_shutdown.py` (`TestTheErrorPathKeepsTheLoop`, the stop
  points) and `tests/test_dashboard.py` (`TestPublishStale`,
  `TestTheStatusBadge`).

- **One position's management error no longer skips the positions after
  it.** *2026-09-26* — `PositionManager.manage_positions` managed the open
  positions in one loop, and an exception while managing any of them raised
  out of it. Every position after it went unmanaged that cycle, its stop
  and target included. The engine then logged an engine error, backed off
  (2 s, doubling to 60 s) for every position and ran no entries that cycle.
  The broker-fill booking that runs first did the same for every position.
  The silent-except cut (under Changed) made this reachable from an exit:
  key_levels' ladder-defence S/R read now raises instead of dropping its S/R
  context. Each position is now managed on its own:
  - The error is logged against the position as
    `Managing AAPL failed (consecutive=N): <step> raised <error>; ...`,
    with its traceback on the first failed cycle of a run and every tenth
    after, and a one-line WARNING in between. The other positions are
    managed as usual.
  - A failed sr_flip manager, adaptive ladder or exit policy (the shared
    exits and the strategy's own) is skipped for that position that cycle,
    and the rest of its management runs. The RiskManager check runs on the
    levels as the failed step left them, so its stop and target still
    fire, and force flatten and the exit order follow.
  - A failure anywhere else ends that position's cycle where it happened:
    the working-exit settlement, the risk check, the bracket sync, or the
    exit order and its booking. A second attempt could double an exit, so
    it is retried next cycle, and a resting broker stop still protects the
    position. One that comes before the risk check (the mark read, the
    working-exit settlement, the risk check itself) costs the position
    that cycle's stop and target check. A position whose bracket fill
    could not be booked is not managed that cycle: its bracket may
    already read down, and the engine would sell again what the broker's
    stop sold.
  - A position whose management fails `runtime.error_escalation_cycles`
    cycles in a row (default 10; 0 turns it off) logs a CRITICAL naming
    it, `POSITION DEGRADED — AAPL: its management failed 10 consecutive
    cycles. Last: ...`, on that cycle and every multiple after. A clean
    cycle, or the position closing, starts the count over.

  A stop signal (KeyboardInterrupt) still ends the cycle. **Behaviour
  change:** such an error no longer fails the engine cycle. It no longer
  counts toward ENGINE DEGRADED, slows the loop or skips that cycle's
  entries, and the dashboard status does not show it.

  **Behaviour change (config): `runtime.error_escalation_cycles` must be
  an integer of at least 0** (0 turns both escalations off); anything
  else refuses to start. The per-position escalation reads it every
  cycle. Before, only the engine read it, after a failed cycle: a null or
  a negative count as 0 (off), `true` as 1, a float as its whole part and
  `"10"` as 10, and a typo raised out of the error path, which stopped
  the bot. Every shipped preset has 10. The README
  (`error_escalation_cycles`) and `_strategies/README.md` (Exits) say so.
  Tests: `tests/test_position_isolation.py` (new) and
  `tests/test_config_validation.py` (`TestRuntimeValidation`).

- **Tests that could not fail now can.** *2026-09-26* — tests only; no
  behaviour changes.
  - `test_properties.py` g2 (the adaptive ladder clears a stale target-exit
    suppress when it re-evaluates) ran `_adaptive_ladder_management` on a
    stand-in that lacked `_ladder_target_strength_confirmed`, and swallowed
    the `AttributeError`. The evaluation never ran: only the reset in front
    of it was tested, so a suppress computed True still passed. It now runs
    a real `PositionManager`, LONG and SHORT, on a tape whose closed bar
    confirms a breakout through the target while the live price is back
    short of it, so `target_reached` alone must clear the flag. The new g3
    keeps the reset covered: a rung the manager cannot read (zero price,
    unreadable price, not a dict) still clears it.
  - `test_scoring_sanity.py`'s rth_trend_pullback monotonicity test skipped
    when no signal came, so a change that silenced the strategy read as a
    skip. Both tapes emit at any clock; it now asserts, like the other
    three. `_relax_entry_gates` sets the two `shared_entry` flags directly
    instead of behind `hasattr` and `except Exception: pass`, so a renamed
    flag fails instead of leaving the gate on.
  - `test_dashboard.py`'s concurrent read/write test passed on a deadlock: a
    stuck thread records no error, and the joins timed out into
    `assert not errors`. It now asserts both (daemon) threads finished, and
    seeds the state so every read checks a payload; the booting state has
    none, and a reader that ran first failed on it.
  - `test_properties.py` f1 / f2 and `test_bug_regressions.py`'s Bug 5 tests
    ran a copy of the dry-run reprice ladder, so no change to
    `SchwabExecutor`'s could fail them, and the two Bug 5 wiring tests
    grepped `submit_option_vertical` / `submit_option_single` for
    `allow_natural_fill=True`: the vertical one still passed with the flag
    set to False, because a comment in the method quotes it. They now send
    orders through `submit_option_vertical` / `submit_option_single` (and a
    single close as `close_position` sends it) in dry run against quoted
    legs, with the new `tests.support.brokers._dry_run_option_fill`: f1 over
    debit verticals and bought options, f2 over credit verticals and sold
    options, and Bug 5 pins the attempt each case fills on
    (`dry_run_fill_attempt_3_natural` below mid, `dry_run_fill_attempt_2` at
    mid) on both submit paths. Breaking any ladder's natural step, either
    submit method's flag (since removed: every dry-run ladder ends on the
    natural, see Changed) or the debit threshold fails them; the old
    versions caught one of those seven.
  - Each of the first three was checked by breaking the code under test:
    the old version passed (or skipped) and the new one fails.

- **Five regression tests that read source now run the code.**
  *2026-09-26* — tests only; no behaviour changes. `test_bug_regressions.py`
  grepped `inspect.getsource` text: the top_tier screen's `.select(...)`
  for `"exchange"`, sr_scalp's stop floor and raw R:R gate lines, the
  partial-breakeven None stamp, risk.py's `is not None` guard (LONG only:
  the SHORT copy of the line kept it passing with either guard removed) and
  the force-flatten branch. They now run the screen through the real
  client against a scan that returns only the selected columns,
  `_build_sr_scalp_signal` (the stop handed on, its gate, and the real
  finalize's refusal), `_adaptive_management_components` on the shipped
  preset, `RiskManager.update_position` on both sides, and
  `PositionManager.manage_positions` (a stop or target keeps its reason in
  the force-flatten window, a scale-out is upgraded). Against fourteen
  mutants of that code the old tests caught eight, the new ones all
  fourteen.

- **The Linux deploy guide's systemd unit now starts the bot.** *2026-09-26*
  — `README_LINUX_DEPLOY.md` ran `python -m intraday_tv_schwab_bot.main`,
  but the package has no `main` module; the entry point is `main.py` at the
  repo root. A unit copied from the guide failed on every start, and
  `Restart=on-failure` retried it every 30s. The `ExecStart` line and the
  `tmux` one-liner under "Quicker alternatives" now run
  `.venv/bin/python main.py --config configs/config.yaml` from the repo root.

- **The README's TA-Lib note matches the pin.** *2026-09-26* — it said
  `requirements.txt` pins `TA-Lib==0.6.8` and that macOS and Linux need the
  C library installed before `pip install`. The pin has been 0.8.0 since
  2026-09-18, and its wheels bundle the C library on Windows, macOS and
  Linux; only a build from source needs TA-Lib C 0.8.1 installed.

- **The `intraday-tv-schwab-bot` console script starts the bot.** *2026-09-26*
  — `pyproject.toml` pointed it at `main:main`, the root-level `main.py`,
  which the wheel does not ship (`packages.find` includes only
  `intraday_tv_schwab_bot*`). The script failed with `ModuleNotFoundError: No
  module named 'main'` after `pip install .`, and after the README's
  `pip install -e .` too, since setuptools 64+ editable installs expose only
  the declared packages. The command line moved to
  `intraday_tv_schwab_bot/cli.py` and the script runs
  `intraday_tv_schwab_bot.cli:main`; `main.py` is now a launcher that calls
  the same `main`, so `python main.py ...` works as before. Tests:
  `tests/test_packaging.py`.

- **`options.underlyings` must be a list (refactor cut C24).** *2026-09-26* —
  a YAML scalar (`underlyings: SPY`) used to load as `['S', 'P', 'Y']`: the
  config normalizer iterated the string, so the 0DTE screeners built three
  one-letter candidates and the options position cap became 3. A scalar,
  mapping or number now fails at load, naming the key. A list reads as
  before. Tests: `tests/test_config_validation.py`.

- **An infinite or NaN value no longer switches off a runner, a quote or an
  option leg.** *2026-09-26*
  - A runner target R of `.inf` / `-.inf` put the target at ±inf. A runner
    then never took profit, or took it on the same check when the target
    landed behind the price (a -inf LONG, a +inf SHORT). The runner
    extension (target, extension R and runner trail) now applies only when
    its candidate target is finite, as the stop tiers already do.
  - A NaN or infinite `initial_stop_price` made the initial risk 0 (NaN) or
    inf, which silently turned off adaptive management, peak giveback and
    the trail's +0.5R activation. It now reads as missing, so the live stop
    anchors the risk. The same holds wherever it is read: the R multiple the
    discretionary exits use, the exit context, and the dashboard position
    risk fall back to the live stop, and the trade record carries no initial
    risk. Tests: `TestInitialStopReadersIgnoreNonFinite`.
  - A quote field of ±inf (Python's JSON parser accepts a bare `Infinity`)
    now falls through to the next key. Before, an infinite mark became the
    management price and exited every equity position on the next check.
  - An option leg rebuilt from a quote read a NaN bid, ask, mark, greek or
    count as 0 / None instead of falling back to the leg's stored value or
    the next key (`mark` then `last`); a NaN greek skipped the stored one.
    The same fall-through now covers inputs no shipped feed produces (quotes
    come from the normalized quote cache and stored legs from
    `OptionContract`): an unparseable string in the quote (it read 0.0, or
    None for a greek), a `pd.NA` (it raised `TypeError`), and a stored value
    that is None, blank or unparseable where the field has another key to
    try (`mark` then `last`, and the `open_interest` / `total_volume` /
    `days_to_expiration` spellings), which read 0.
  - Tests: `tests/test_properties.py` (n8, n11, n12) and
    `tests/test_bug_regressions.py` (`TestPartialBreakevenTier2026_04_23`).

- **Four numeric reads no longer mis-read NaN or float strings (refactor cut C16).**
  *2026-09-26*
  - A quote field that is NaN (Python's JSON parser accepts a bare `NaN`)
    now falls through to the next key, as a missing one does. Before, a NaN
    `bidPrice` became the bid, and a NaN mark or last became the mid.
  - The entry snapshot drops a warm-up NaN `vwap` / `ema9` / `ema20` /
    `ret5` / `ret15` instead of writing `NaN` into the ENTRY_CONTEXT JSON.
  - Adaptive management read a NaN tier knob as NaN. A NaN breakeven offset
    left the stop where it was but marked the tier armed, and a NaN runner
    target turned a runner's missing target into NaN. A NaN knob now reads
    as its default, so a NaN offset moves the stop to entry. A tier whose
    candidate stop is infinite (an `.inf` offset) now neither moves the stop
    nor arms; before, the stop went to ±inf and the position stopped out on
    the same check.
  - Option-chain counts sent as float strings (`"12.0"`) read as 12, not 0,
    so the open-interest and volume floors no longer drop them; a
    days-to-expiration of `"0.0"` reads as 0, not the -1 / 0 default.
  - Tests: `tests/test_properties.py` (n7-n10) and
    `tests/test_bug_regressions.py` (`TestPartialBreakevenTier2026_04_23`).

- **A dry run no longer replaces a live broker order.** *2026-09-25* —
  `SchwabExecutor.replace_bracket_child` had no dry-run guard, unlike
  `submit_protective_oco` and `cancel_bracket`. In bracket mode a dry-run
  restore adopts the REAL stop resting at the broker, and resizing it went
  through that path, as did every trail sync after it. So a paper bot
  replaced the user's stop at the broker, at the bot's own size and price.
  The old id then read as a foreign order and blocked entries for the day.
  A dry run now logs the replace and sends nothing. Bracket mode is off in
  every preset.
  - Tests: `tests/test_sweep_fixes.py` (`TestDryRunNeverReplacesALiveOrder`).

- **A broker read that fails no longer reads as an empty account.**
  *2026-09-25* — the startup and session-boundary reconcile read
  `account_details` without checking the status, and the entry-block recheck
  did the same. A JSON error body (the 401 a lapsed token returns) parsed
  fine and held no positions, so:
  - at the session boundary every tracked position was booked
    `closed_outside_bot`, its broker bracket was cancelled while the broker
    still held the shares, and it was dropped from tracking;
  - at startup, `restore_hybrid` took the empty branch and pruned every
    reconcile metadata row, which the next restart needs;
  - the entry-block recheck cleared the block for an ignored open symbol.

  The reads now go through `SchwabExecutor.fetch_account_positions()` and
  `fetch_working_orders()`, which return None on any failure. Every read the
  reconcile makes now fails the attempt when it cannot be read, and the
  engine retries it (next entry):
  - An unread account: nothing is settled, cancelled, restored or pruned,
    and the blocking modes block entries (`startup_reconcile_failed`). Every
    ignore-list symbol is blocked, in every mode, until its own recheck
    reads the account: the holdings are unknown. The recheck keeps its
    block on a failed read.
  - An unread working-order list: the settle still runs (it reads only the
    account), then the attempt fails with `startup_reconcile_failed`. In
    bracket mode the restore waits for the list. Without it, the restore
    could not see the stop still resting from before the restart and placed
    a second one beside it; a restored symbol is never revisited, so both
    stayed, and the pair sells the position net short when both trigger.
    This was already so for an HTTP error, and a dropped connection or an
    undecodable body used to fail the whole reconcile. Without bracket mode
    the list protects nothing, so the restore runs and the engine manages
    what the broker holds while the retry waits for the list. The
    `orders_lookup_failed` result key and the `orders_lookup_failed` /
    `startup_reconcile_orders_lookup_failed` block reasons are gone.
  - A tracked position's working exit order whose state cannot be read:
    the settle leaves the position tracked, and the attempt fails and blocks
    entries in the blocking modes. The position used to stay at the wrong
    size all day, its exits selling shares that were not held.
  - A saved position's working exit order whose state cannot be read during
    a hybrid match: the attempt fails before that position is restored. It
    was read as "no fills", so the position was restored basic at the broker
    quantity with its stop resized to all of it, beside the shares the
    order still sells, and the order was lost for good. A dry run reads no
    order state by design, so there the row still just does not match.
  - A foreign working order whose state cannot be read still counts
    (`working_orders_present`), but the attempt fails. It used to succeed,
    and the block held for the day against an order the settle had already
    cancelled. A dry run skips this check, since it retires nothing at the
    broker.

  The ignore-list check reads a broker row with `broker_position_side_qty`,
  as the settle does: a malformed quantity no longer fails the whole
  reconcile, and a row both long and short is not held.
  - Tests: `tests/test_startup_reconciler.py` (`TestAccountReadFailsClosed`,
    `TestExecutorAccountReads`, `TestIgnoredOpenPositionParsing`) and
    `tests/test_sweep_fixes.py` (`TestReconcileOnAnUnreadOrderList`,
    `TestReconcileOnAnUnreadOrderState`, `TestSessionReconcileBesideAWorkingExit`,
    `TestHybridMatchAcrossAWorkingExitsFills`).

- **A failed reconcile is retried instead of blocking the whole day.**
  *2026-09-25* — `reconcile()` swallowed every exception, and the engine
  then marked the day as reconciled, so its "will retry" branch never ran.
  One network blip or 5xx during the 07:00 session-boundary reconcile, or at
  startup, blocked entries (`startup_reconcile_failed`) until the next ET day
  or a restart. `reconcile()` now returns whether it read the broker, and
  the engine stamps the day only on success.
  - The retry fires on trading days inside the stream window. The first
    retry comes 60 seconds after the failed attempt ENDS
    (`RECONCILE_RETRY_SECONDS`). The interval doubles with each failure in a
    row, up to 5 minutes (`RECONCILE_RETRY_MAX_SECONDS`). An attempt is at
    least two Schwab reads, plus order-state reads, and a read that hangs
    takes about 30 seconds on the engine thread. The first success clears
    the block.
  - The retry runs in every mode, `log_only` included, since an unread
    account there still leaves positions closed overnight unsettled. It also
    runs with `session_reconcile_on_resume: false`, which now turns off only
    the new-day re-run.
  - Until a reconcile has succeeded, a metadata save writes the tracked
    positions over their stored rows and deletes none
    (`ReconcileMetadataStore.upsert_positions`). A failed startup attempt
    restores some positions, or none, and the first management cycle's save
    then replaced every stored row with that set, so the retry restored the
    rest basic, or skipped the positions that need metadata. The first
    successful reconcile replaces all the rows.
  - Tests: `tests/test_startup_reconciler.py` (`TestEngineReconcileRetry`).

- **A hybrid reconcile no longer prunes the metadata of a position it still
  tracks.** *2026-09-25* — a `restore_hybrid` reconcile deleted every
  metadata row it did not match, and it never matches a tracked symbol (the
  restore skips it). The session-boundary re-run therefore deleted the rows
  of positions held overnight, and the engine's save, which skips an
  unchanged position set, did not rewrite them. A crash before the position
  next changed restored it with `restore_basic` defaults: its levels,
  bracket ids and working-exit order were lost, or it was skipped outright
  when the strategy requires hybrid metadata. The empty-broker branch did
  the same to positions the settle leaves tracked. Both prunes now keep the
  rows of tracked positions. The Phase A retry would otherwise have repeated
  the prune.
  - Tests: `tests/test_startup_reconciler.py` (`TestAccountReadFailsClosed`).

- **The settle books a bracket's own fills, and never stacks protection
  beside a cancel it cannot confirm.** *2026-09-25* — the settle booked the
  whole gap between the tracked and the held quantity as
  `closed_outside_bot` at the last mark. A bracket stop that filled after
  the last management cycle therefore read as an outside close (a stop
  filled at 3.94 against a 4.20 mark was booked as a gain), and the risk
  manager never saw the exit. It then cancelled the bracket and, even when
  the cancel was not confirmed, placed fresh protection beside it; both
  stops sold the position net short.
  - The bracket is cancelled first. What its children filled is booked
    through `PositionManager.book_bracket_cancel_fills`, at the broker's
    price with the risk manager's registration, and only the rest of the
    gap is booked outside.
  - A cancel that cannot be confirmed leaves the position as it was:
    nothing booked, nothing placed. The attempt fails, and the retry sends
    the cancel again, as the manager's cancel-before-exit does. Until then
    the position is held (`settle_pending`): the manager sends it no exit and
    no re-protect sized to shares the broker no longer holds. The first
    retry after a hold comes after 10 seconds
    (`RECONCILE_SETTLE_RETRY_SECONDS`), whatever failed before it, and the
    delay doubles only while the hold lasts. Only an attempt that can read
    the position again lifts the hold.
  - When the cancel reports fills, the account and then the working exit
    order are read again once the bracket is down. A child, or the working
    exit, can fill between the first reads and the cancel. Sizing from the
    first reads kept, and re-protected, shares the stop had already sold,
    and booked a slice's fill twice.
  - The re-protect adopts a stop still resting for the position (one moved
    in the app) instead of placing fresh protection beside it. It uses the
    saved stop while the order list still shows it. Otherwise it uses the
    stop resting for the position, or, when none rests, the saved one, which
    is adopted only if its own state read shows it live or cannot be read.
    The children the settle just cancelled are left out of the list. A
    bracketed position that keeps shares therefore waits for the order
    list; one the broker no longer holds does not.
  - The estimated loss of a close outside the bot now counts toward
    `max_daily_loss`; an estimated gain does not, since the mark can be
    stale. It had been left out because "the close happened outside this
    session", but the reconcile now also runs mid-session on a retry.
  - Tests: `tests/test_sweep_fixes.py` (`TestSettleBooksTheBracketsOwnFills`,
    `TestSettleAdoptsAStopMovedInTheApp`, `TestDryRunProtectionIsSimulated`).

- **A dry run's broker protection is simulated.** *2026-09-25* — a
  dry-run restore adopted the REAL resting stop as the paper position's
  active bracket. RiskManager left the stop to the broker, but a dry run
  reads no order state and never saw that stop fill, so the paper position
  never exited on its stop.
  - `ensure_position_protected` now returns a simulated bracket in a dry
    run, adopting, resizing and submitting nothing, so the engine owns the
    exits. It keeps the ids of the real protection, which the reconcile
    counts as the position's own instead of as a foreign order that blocks
    every entry.
  - A saved row that tracks a working exit order no longer matches in a
    dry run, which can never settle that order. Restored with it, the paper
    position was never managed again, and every cycle tried to cancel the
    user's real order; only `cancel_working_order`'s dry-run guard stopped
    it.

- **An entry order still settling is left to the entry gatekeeper at the
  reconcile.** *2026-09-25* — the account holds an unsettled entry order's
  fills before the gatekeeper books them.
  - The restore adopted the same fill a second time. The gatekeeper then
    grew the restored position by the same shares, recording the entry
    twice, and in bracket mode resized its stop to twice what was held.
  - The settle read an outside close short by the late fills. A position
    closed entirely in the app was dropped, and the gatekeeper then adopted
    its late fills as a new position the broker no longer held.

  The fix:
  - The reconcile now books unsettled entry orders from their own fill
    records before it reads the broker.
  - Anything still unsettled is left alone: the restore skips it, the settle
    skips it, and no working order is judged.
  - The attempt fails (`startup_reconcile_failed`, naming the orders) and is
    retried as soon as the gatekeeper has booked them.
  - A restart clears an order that never settles, and the restore then
    adopts its fills.
  - The session-boundary reconcile also runs before the cycle now. The first
    premarket cycle used to manage, and send exits for, positions closed
    overnight before the reconcile dropped them.
  - Tests: `tests/test_startup_reconciler.py`
    (`TestUnsettledEntriesAtTheReconcile`, `TestEngineReconcileRetry`).

- **A bracket whose stop died at the broker is retired.** *2026-09-25* — a
  DAY child expires at its session's end (09:25 for AM, 16:00 for NORMAL),
  and a stop can be cancelled in the app. The bracket still read active,
  so RiskManager deferred the stop to an order that no longer rested. The
  position had no broker stop and no engine stop.
  - The fill reconcile now retires a bracket whose stop reads EXPIRED,
    CANCELED or REJECTED. What else of it rests is cancelled, what that
    cancel reports filled is booked, and the position is re-protected, or
    the engine owns the stop.
  - A REJECTED stop is never placed again. The broker rejects the same
    order again, and each rejected replacement read as protection that
    suppressed the engine stop, every cycle. A stop is also re-placed at
    most once per bracket.
  - When that cancel cannot be confirmed, the rest of the bracket (a target
    the OCO no longer links) stays tracked, so its fill is still booked and
    the cancel-before-exit still sends its cancel. Only the dead stop is
    dropped, so the engine owns the stop at once. Only the dead stop's own
    fills are booked then, recorded on the bracket
    (`booked_child_fills`) so that a later cancel of the wrapper, which
    still lists the dead stop, does not report them again. The rest's are
    booked once, when their cancel confirms or they fill. Once the rest is confirmed down (or dies itself),
    the dead stop gets its one fresh placement, unless it was REJECTED.
  - A REPLACED stop is not dead: its replacement may rest under an id the
    bracket could not be re-pointed to.
  - The fill reconcile also reads a child the 8-hour listing no longer
    returns on its own. A stop placed before 07:00 lives until 16:00, and
    its fill after 15:00 was never booked. A live child is read at most
    once a minute (`UNLISTED_BRACKET_CHILD_READ_SECONDS`).
  - A working exit order that closes the position now takes its leftover
    bracket down. A remainder stop left resting beside it opened a new
    position on trigger.
  - Bracket mode is off in every preset.
  - Tests: `tests/test_bracket_orders.py`
    (`TestBracketChildrenTheListingMisses`, `TestAStopThatDiedAtTheBroker`).

- **One reading of a Schwab response.** *2026-09-25* — status checks used
  four conventions: `status >= 400` (a 3xx, or a response with no status,
  passed), `200 <= status < 300` with a missing status read as 0 or as 200,
  and a bare `response.status_code` that raised. Three reads checked nothing.
  Now `schwab_api.response_ok` (2xx only; a missing status is a failure) is
  the only status reading, and `call_schwab_json` also requires a body that
  decodes, raising `SchwabHTTPError` otherwise. The data-feed price-history,
  quote and batch-quote reads, the account-hash lookup, every order status
  check in `execution.py` and the bracket reconcile's `account_orders` read
  use them.
  - 0DTE option chains: an error body parsed to an empty chain, which was
    cached for `option_chain_cache_seconds`, so the build skipped entries as
    `option_chain_empty`. Now an error is not cached as a chain. The build
    reports `option_chain_unavailable`, and the symbol is re-read after
    `option_chain_cache_seconds`, the same pace as a good chain, so a
    failing chain costs no extra Schwab calls. An HTML error body no longer
    fails the whole engine cycle.
  - Tests: `tests/test_schwab_api.py`.

- **top_tier's Fix G lets a first ladder rung sit on the nearest level.**
  *2026-09-25* — both top_tier_adaptive and small_cap_squeeze ship
  `adaptive_ladder` mode. In that mode Fix G (`reject_target_beyond_sr`)
  refused every laddered trend entry, so trend traded only as a trail
  runner: in top_tier since 1.0.0, and in small_cap_squeeze since it
  shipped. The cause is how the rungs are built. The rung builder draws
  them from the S/R list that `nearest_resistance` / `nearest_support`
  heads, so rung 1 always sat at or past the nearest level. The target /
  S/R ratio was therefore at least 1.00, above `target_max_sr_ratio` (0.7
  top_tier, 0.8 small_cap_squeeze).
  - A first rung ON the nearest opposing level now passes (top_tier
    `_finalize_signal`, with a 1e-6 tolerance for the builder's
    `round(price, 6)`). That rung is the ladder's own take-profit at that
    level, and the ladder manages it.
  - A first rung past a nearer level is still refused (ratio above 1.00).
    That nearer level did not qualify as a rung because its R:R was under
    `ladder_min_target_rr`.
  - Unchanged: non-ladder mode, runners (no target), and the range /
    pullback exemptions. No preset changes, and Fix G stays on.
  - Live evidence (`decisions.csv`, 04-27 to 09-21): there were 25 Fix G
    episodes (top_tier 19, small_cap 6), and every one had a ratio of 1.00
    or more. The 10 at exactly 1.00 (top_tier 8, small_cap 2) would now
    trade.
  - Replay: 1 more top_tier trend entry in 11 sessions (NVDA 09-24 10:25
    SHORT, which the replay walk stopped out at -1R). Laddered small_cap
    trend signals on the synthetic tapes go from 0 to 12.
  - The outcome evidence is thin and mixed, so watch trend entries in the
    next dry-run. At-level proxy walks give top_tier +0.29R (n=7) with an
    ATR stop and -0.03R (n=8) with a 1% stop; small_cap's XOS was -1R.
  - Correction: the COST 2026-04-24 09:56 LONG that motivated Fix G was a
    trail runner with no target. Its only resistance, 1014.94, sat under
    the 1.2R rung floor, so Fix G never covered its own incident (see Known
    issues).
  - Tests: `tests/test_knob_reach_matrix.py`, long and short through the
    real rung builder.

- **LTF market structure no longer confirms pivots with the still-forming
  bucket.** *2026-09-25* — top_tier_adaptive and small_cap_squeeze resample
  the 1m stream to 5m for their LTF structure
  (`structure_ltf_timeframe_minutes: 5`), and the peers read `get_merged`'s
  native 5m LTF frame. Both frames keep the partial last bucket, and it
  could serve as a pivot's right-hand neighbour. A pivot could therefore be
  confirmed by 1-4 minutes of a 5m bar and be gone when the bar closed: 9
  of the 32 pivots the forming bucket confirmed at a top_tier entry had
  vanished by then.
  - `analyze_market_structure(..., last_bar_forming=False)` leaves the last
    bar out of the pivot search only. The close, the ATR and the BoS /
    CHoCH crosses still read the whole frame, so a break on the forming
    bucket counts at once.
  - `BaseStrategy._structure_context` sets the flag with the dashboard's
    clock test: the last bar's bucket ends after now. The bar length is the
    resample's, otherwise the new `utils.frame_bar_minutes` (the smallest
    positive label step). Using the smallest step means a thin 1m name's
    completed last bar is never read as forming. A frame of completed 1m
    bars is unaffected, and a tz-naive index is read as ET wall time.
  - A step into a label that opens a session segment (09:30, the close,
    20:00) does not count toward that length, because the grid cuts the
    bucket before it short. A native 60m frame steps 09:00 -> 09:30, and
    the plain smallest step would read it as 30m and call its forming
    bucket complete halfway through. No preset runs a 60m (or 45m / 90m)
    native LTF.
  - The dashboard's 5m LTF structure overlay does the same
    (`DashboardCache.current_structure_overlay(..., last_bar_forming=...)`).
    Its chart patterns still read the forming bucket, as the strategy's do.
  - Effect: 4 of 162 archived top_tier entry verdicts change (3 unblocked,
    1 newly blocked; net about +0.26R, which is noise). An exit replay of
    194 top_tier / small_cap trades (24 sessions, 4,176 management bars)
    changed 0 exits. The structure context differs on 15.6% of those bars,
    but no exit decision does. The peers' entry-side effect is unmeasured.
    This fix does not address the +7.43R the 5m structure veto blocks (see
    Changed: the range / pullback / sr_scalp exemption).
  - `_structure_context` now reads the clock. A test tape that ends after
    the wall clock sees its last 5m bucket as forming unless the test
    freezes `strategy_base.now_et`.
  - The structure veto's same-side-BoS escape is unchanged on both sides.
    Its LONG clause could not fire while the resolver read every close
    through an inverted reference pair bullish; the inverted-pair fix
    below makes it reachable, in the mirror of the SHORT case.
  - Tests: `tests/test_structure_forming_bucket.py`.

- **zero_dte_etf_options' credit pivot-buffer gate works, and ships off.**
  *2026-09-25* — it never fired, although the preset shipped
  `credit_pivot_buffer_gate_enabled: true`. It read the
  `msltf_` / `mshtf_reference_*` pivots from the top level of
  `_regime_confirm`'s result, but they live under `regime['metrics']`.
  - It now reads them from `regime['metrics']`, the dict the signal stamps
    as `regime_metrics`.
  - It refuses a short strike within `min_short_strike_pivot_buffer_atr` x
    ATR of the OUTERMOST pivot: the higher of the LTF / HTF reference highs
    for a bear call, the lower of the reference lows for a bull put. The
    refusal reason is
    `midday_credit_spread_unavailable(reason=short_strike_too_close_to_pivot(...))`.
  - The preset now ships `false` (see Changed), so nothing changes under
    any preset.
  - If enabled, it would refuse 32 of the 38 credit entries in the fixture
    replay; in 29 of them the short sits between spot and the outer pivot.
    It would also refuse 4 of the 8 live ones (2026-05-20..22, net -$22,
    including a +$40 target winner). Enable it only after a dry-run.
  - The dead `regime.get("reasons", [])` read in its `entry_signals` is
    removed. `_regime_confirm` sets only `reason`, which already joins
    every reason, so there is no behaviour change.

- **The same-level retry block keys an option on its underlying.**
  *2026-09-25* — two defects in one path.
  - Units: for an option the block compared premiums (dollars per
    contract, in $1 steps) with `same_level_block_atr_mult` x the
    underlying's ATR ($0.06-$0.23 in the archive). It therefore fired only
    on an exact premium match, whatever the underlying did.
  - Side: it matched on the ORDER side. That blocked a flip between two
    credit spreads (both are sold) and missed the same bullish bet made
    first through a credit spread and then through a debit.
  - `RiskManager.same_level_anchor(strategy, side, metadata, price)` now
    gives the (direction, level) the block keys on. It reads a signal and
    the position it became the same way:
    - an equity: its side and entry;
    - an option: its market direction (`metadata['direction']`, bullish* /
      bearish*) and the underlying's price at entry (`underlying_entry`).
  - The record logs the underlying's close at exit, so the whole record
    sits in the underlying's price space. When no level can be read, the
    block skips; the cooldown still applies.
  - `register_exit(..., level=(Side, price))` replaces `entry_price=` (a
    clean break). `side=` still keys the cooldown on the order side. Every
    exit registers through `PositionManager._register_closed_position`, and
    the persisted `RecentExitRecord` schema is unchanged.
  - Both option presets now ship `risk.same_level_block_minutes: 0` (see
    Changed). The old block never fired on an option in the archive (0
    `same_level_retry_block` refusals over 2026-05-18..22). The corrected
    one at 30 / 0.3 would not have fired either: two of the re-entries were
    flips, and the other two sat 0.46 and 0.49 ATR from the prior entry.
  - The 20-minute candidate cooldown already blocks the underlying in both
    directions, because zero_dte candidates carry no directional bias.
  - Equities are unchanged: their anchor is (side, entry), as before. A new
    test pins that the block works for rth_trend_pullback /
    volatility_squeeze_breakout if it is switched on; both ship 0.
  - The block's comments in `config.py` and the 15 equity presets said it
    fired after a stop-out or losing exit and keyed on the prior stop or
    fill. It records every exit and keys on the prior entry; the comments
    now say so. The `_fib_pullback_override` docstring no longer calls an
    option's entry its premium: it is the underlying's, and the override
    stays off for options because their side is the order side.
  - Tests: `tests/test_option_same_level_block.py`.

- **A restart while an exit order is still working at the broker.**
  *2026-09-25* — six edges on one path. All are latent under the shipped
  dry-run presets: a simulated exit leaves no order to track, the
  session-boundary settle is skipped in dry run, and bracket mode is off by
  default.
  - The session-boundary re-reconcile booked fills of a position's own
    tracked working exit as `closed_outside_bot`, at the last mark. The
    next cycle booked them again from the order's record and dropped a
    position the broker still held. The settle now nets those fills out,
    and the manager books them once, at the broker fill price, under the
    exit's reason. A position whose order state cannot be read stays
    tracked.
  - A restored position's own working exit counted as a foreign order.
    `working_orders_present` then blocked every entry for the session, and
    "clear them" meant cancelling the position's own exit. The position now
    owns that order.
  - A snapshot order the reconcile itself retired counted as foreign too.
    That covers a child the restore REPLACED with a resize, and the bracket
    the session-boundary settle cancelled for a position partly closed
    outside the bot. The settle case restores nothing (a tracked symbol is
    skipped), so the filter now runs whenever foreign orders remain, not
    only after a restore. One `fetch_order_states` call drops REPLACED,
    terminal and filled orders. That listing looks back 8 hours while the
    snapshot covers `startup_order_lookback_days`, so an id it does not
    return (a stop entered before an overnight hold) is read with
    `order_details`; read as live, it stayed foreign. An order whose state
    cannot be read, from the listing or on its own, is kept.
  - `restore_hybrid`'s metadata match needed the broker quantity to equal
    the saved one. If the working exit sold shares while the bot was down,
    the position fell back to `restore_basic` defaults and lost the order,
    its one-shot marker and the slice's P&L. The match now bridges the gap
    with the order's unbooked fills and restores at the saved quantity. The
    first cycle books the fills.
  - In bracket mode, the restore protected the full quantity beside a
    working slice, so the resting stop plus the order covered more shares
    than were held. A stop that triggered while the order worked left the
    position net short. Beside a working full exit, it placed a fresh
    full-size OCO. The restore now protects only the shares outside the
    slice (the manager's `_reprotect_beside_working_order` sizing) and
    places nothing beside a full exit. That sizing holds while the order is
    live. An exit order that died while the bot was down (cancelled in the
    app, a DAY order that expired) covers only its unbooked fills, so the
    shares it no longer covers get the stop at once rather than a cycle
    later. An order whose state cannot be read counts as live.
  - An exit result with `ok=True` and `filled_qty=0` skipped the not-filled
    routing. It neither tracked the order nor re-protected, so the bracket
    cancelled that cycle stayed down. It is now settled as the unfilled
    attempt it is (EXIT_CONTEXT `attempt_status: filled_qty_zero`). Today's
    executor cannot produce this result, because it sets `ok` only on a
    fill.
  - New helper `broker_positions.working_exit_outstanding_qty`, shared by
    the manager's risk check beside a slice and by the restore.
  - `restore_basic`, which has no saved metadata, still cannot see a
    working exit (see Known issues).

- **The HTF divergence reads the configured thresholds.** *2026-09-25* —
  `MarketDataStore.get_htf_context` passed `build_htf_context` only
  `enabled`, `divergence_enabled` and `htf_divergence_max_age_bars`. The
  HTF RSI divergence therefore always used the builder's defaults for
  `divergence_pivot_lookback`, `divergence_min_price_move_pct` and
  `divergence_rsi_min_delta`, and retuning them moved only the LTF
  divergence.
  - The data feed now passes all three from `technical_levels`, with no
    `or` fallback; the builder clamps them the way the LTF builder does.
    They are part of the HTF cache key.
  - No build changes: all 19 configs load 4 / 0.0015 / 2.5, which are the
    builder defaults.
  - `divergence_rsi_length` stays LTF-only; the HTF reads its frame's
    `rsi14`.

- **The shared FVG score term uses the strategy's HTF EMA spans.**
  *2026-09-25* — it built its HTF context from
  `support_resistance.ema_fast_span` / `ema_slow_span`. Those fields do not
  exist, so it always asked for 50/200.
  - It now uses `htf_ema_spans(params)`. Its request equals
    `_default_htf_context_for_score`'s except for the FVG arguments.
  - FVG lists and scores are unchanged, because FVG detection never reads
    the EMAs. They were identical on the fixture 60m frames.
  - The cache key is unchanged for the 50/200 presets. The peers' key
    (34/200) moves from EMA 50 to 34, with the same number of entry-path
    HTF builds.
  - Side effect: while the dashboard shows a `peer_confirmed_htf_pivots`
    symbol, its generic sidebar HTF read (hard-coded 50/200) no longer
    shares the FVG term's build. That is about one extra ~6 ms HTF build
    per symbol per 60m bar. This comes from reading the code and was not
    measured.

- **An inverted reference pair resolves to its later break.** *2026-09-25*
  — `_resolve_structure_bias` tested a close through the reference high
  before one through the reference low. Both hold only on an inverted pair
  (the reference low two breakout buffers or more above the reference
  high), so every close through both read bullish, whichever way price had
  gone.
  - Where the pair comes from: a pivot needs neighbours in its own session,
    so a gap's first bar is never one. After a gap up the first swing low
    forms above the old reference high before any high confirms; after a
    gap down, the mirror.
  - A gap up that then broke its first swing low (a BoS down) read bullish,
    and so did its exact mirror image, a gap down that broke its first swing
    high. The second reading is right, the first is not: the tilt was a
    one-sided bullish one.
  - The later of the two breaks (the smaller BoS age) now decides. A price
    passed in that is through a reference the frame's last close is not
    through counts as the newer break. Equal ages decide nothing, and the
    rest of the resolver runs.
  - The structure veto's same-side-BoS escape can only matter on such a
    pair, where the bias is the later break and the earlier one can still
    be fresh. Both of its clauses are unchanged. Until this fix only the
    SHORT one could fire; the LONG one is now reachable in the mirror case.
  - Effect, all on archived tapes: 0 of the 162 top_tier entry verdicts
    change (no entry had an inverted pair). Over every completed 5m bucket
    of 691 symbol-days (67,891 bars, the top_tier / small_cap_squeeze LTF
    structure params), 21 bars had a close through both references, and 17
    of them now read bearish instead of bullish. All 17 were premarket
    (GOOG, INTC, MRVL, PLTR). The SHORT escape fired on 13 bars before the
    fix, all of them among those 17, and on none after it; the LONG one
    fires on none. On the 15m HTF structure, 4 of 23,329 bars had a close
    through both and 2 flip, both at 09:45 (AAPL 07-28, INTC 05-11).
  - Tests: `tests/test_structure_forming_bucket.py` (the gap up and its
    mirror, a mirror-symmetry property of the resolver, the escape on both
    sides).

- **An adopted stop older than the order listing is read and resized.**
  *2026-09-25* — `execution._adoptable_protection` read each child's state
  from the account_orders listing only, which looks back 8 hours. A stop
  entered before an overnight hold is not in it. It was adopted with no
  size, so `ensure_position_protected` never resized it: `restore_basic`
  left a 10-share stop resting against 7 held shares, which would have sold
  3 more than were held when it triggered. Latent: bracket mode ships off.
  - A child the listing does not return (or every child, when the listing
    fails) is now read with `order_state` (`order_details`), the same
    fallback `startup_reconciler._drop_retired_orders` uses. A dead stop
    is no longer adopted, and a dead target is dropped from the adopted
    ids.
  - A stop neither read returns is still adopted, never stacked on, but its
    size is unknown, and an unknown size is now re-issued at the position's
    size (fail closed). If that replace fails too, the bracket stays active
    with `state: qty_unverified` and `qty: None`, and the error is logged.
  - Tests: `tests/test_sweep_fixes.py` (`TestAdoptionBeyondTheOrderListing`,
    including `restore_basic` end to end).

- **Leftovers of the 2026-09-25 fixes.** *2026-09-25* — latent under every
  preset.
  - The LTF divergence readers (`strategy_base._technical_context` and the
    dashboard's LTF build) read `divergence_rsi_min_delta`,
    `divergence_pivot_lookback` and `divergence_min_price_move_pct` as
    `x or <default>`, so a configured 0 meant the default, while the HTF
    build honours it. They are read None-aware now: 0 is honoured and only
    null falls back. No preset sets any of them to 0.
  - `zero_dte_etf_options._htf_fvg_context_request` (inherited by
    zero_dte_etf_long_options) read the missing
    `support_resistance.ema_fast_span` / `ema_slow_span`, so it always asked
    for EMA 50/200. It resolves the strategy's spans with `htf_ema_spans`
    now. zero_dte declares no HTF spans, so it asks for 50/200 either way,
    and the prefetch still warms the same cache key.
  - `zero_dte_etf_long_options` dropped its dead
    `regime.get("reasons", [])` read in `entry_signals`, as
    zero_dte_etf_options did. `_regime_confirm` sets only `reason`.
  - Tests: `tests/test_shared_entry_policy.py`,
    `tests/test_fix_dashboard_charting.py`, `tests/test_htf_ema_knobs.py`,
    `tests/test_zero_dte_shared_entry.py`.

- **Dashboard: a divergence line whose older pivot is off the chart keeps
  its slope.** *2026-09-25* — `drawDivergenceLine` put a `pivot_a` older
  than the chart's first bar on bar 0. The line then started at the left
  edge at a slope the divergence never had. A line with both pivots
  off-window left a lone "RSI ÷" label on bar 0.
  - `pivot_a` is now placed `pivot_b.pos - pivot_a.pos` bars before
    `pivot_b` and keeps its price. The line is clipped to the plot, and the
    label sits at the midpoint of the visible part.
  - A line is not drawn when its `pivot_b` is off-window too, or when the
    positions cannot place `pivot_a` left of the window.
  - Since the 09-24 session-bar age, a divergence can pair yesterday's
    pivots at the open. On the peers' 90-bar 5m chart, 92 of 245 line-reads
    between 09:30 and 10:29 had an off-window `pivot_a`; the other charts
    had 0 of 503.

- **The exit tape no longer vetoes every exit on a first session bar, a
  zero-range bar or a NaN reference.** *2026-09-24* — each of these read as
  a veto in both directions:
  - On the first bar of the indicator session (09:30, or 07:00 for an
    extended preset) the session EMAs equal the close. That bar now reads
    `ema9_all` / `ema20_all`, and the session VWAP, which that bar alone
    defines, abstains.
  - A zero-range bar's close position (0.5, which fails both 0.46 and 0.54)
    abstains.
  - A NaN reference used to fall back to the close; it abstains too.
  - If every enabled term abstains, the tape confirms nothing.
  - The `candle_pattern` family holds when any of the last 3 bars is a
    single print (the new `bar_range` gate, the same window as the entry
    candle veto, `helpers.CANDLE_PATTERN_WINDOW_BARS`). TA-Lib reads a
    single print as a doji / white candle,
    so thin tape builds TRISTAR, DOJISTAR, HARAMI, HIKKAKE or
    GAPSIDESIDEWHITE by itself; the old 0.5 veto hid that. Without the gate,
    a candle exit would fire on about 6.5-10% of premarket zero-range bars.
  - These are correctness fixes; no measured P&L effect. The exposure is in
    extended hours: zero-range bars were 10 of 113 held minutes for
    small_cap_squeeze and 0.13% for top_tier.

- **The entry candle veto abstains on single prints.** *2026-09-24* — a
  single-print bar (high == low) is the artifact the exit side's `bar_range`
  gate holds on (above): TA-Lib reads it as a doji / white candle. The entry
  veto had no such guard, and it ships on for `microcap_pm_breakout`, whose
  07:00-10:25 window is mostly premarket. On 34 archived small-cap
  symbol-days its LONG veto fired on 19.6% of premarket minutes, and 246 of
  those 609 vetoes (40%) matched only TRISTAR / GAPSIDESIDEWHITE /
  HARAMICROSS-type patterns with a single print among the last 3 bars. The
  veto now abstains when any of the gate frame's last 3 bars is a single
  print or has an unreadable high / low (`helpers._bars_have_range`), and the
  signal carries `shared_entry_candle_abstained: zero_range`. Three bars, not
  the last one: the veto was rarer when only the last bar was flat, so the
  artifact sits in the pattern window. In RTH only 16 of 321 vetoes were
  artifact-only.

- **The engine pre-warms only the contexts its bars frames are read for.**
  *2026-09-24* — every context-builder call was recorded for
  `_prime_cycle_context_cache`, which then built that context on every
  watchlist symbol's 1m frame each cycle. The caches key on the frame
  object, so a build on any other frame (the peers' 5m LTF, key_levels_1m's
  `get_merged` copy) never read the pre-warm. `admit`'s builds on
  key_levels' LTF registered the chart and technical contexts: a few ms per
  symbol per cycle that nothing read. htf_pivots and trend_continuation
  already paid for unused 1m builds. The engine now hands the strategy the
  cycle's bars frames first (`BaseStrategy.set_prewarm_frames`), and only a
  build on one of them registers. The context caches are also reset every
  cycle when nothing is observed; every entry pins its frame.

- **The CHoCH exit needs a CHoCH that happened after entry — and no other
  gate.** *2026-09-24* — it read a CHoCH from the last
  `structure_event_lookback_bars` bars without asking when it happened, so
  a LONG opened within six bars after a 1m CHoCH down exited on its first
  weak-tape cycle. The event must now close after the entry. CHoCH is an
  ungated structural stop-tightener: it fires below about -0.4R on roughly
  0.5-1.3% of positions, and the study measured it neutral on 5m structure
  and slightly negative on 1m structure. No candidate guard had a
  measurable effect (an R floor, post-entry pivots, the hold or ORB grace;
  the grace was -5.1R [-15.3, +5.3] on 1m breakout entries), so it has
  none. The "a true CHoCH is a reversal signal" rationale was rewritten
  everywhere.

- **Structure events and pivots are judged by when their bar CLOSED.**
  *2026-09-24* — bars are labelled at their start, so a CHoCH, BoS or pivot
  on the bar the entry filled in read as pre-entry for as long as price
  stayed through the level. That window was up to 5 minutes on a 5m
  structure frame. The new `shared_exit.bar_closed_after` is used by the
  CHoCH / BoS post-entry checks, the pivot guard and the divergence
  scale-out. The pivot guard now counts `MarketStructureContext.pivot_times`
  closed after entry; it used to compare against an entry-time count only
  some strategies stamped, so for the peer family it could never open.

- **The S/R-loss exit could never fire; it is reachable now and ships
  off.** *2026-09-24* — it sat behind `discretionary_exit_min_r`, but it
  fires only with price through a level on the ADVERSE side of entry (R < 0
  by construction), so it was dead in every preset. It is now exempt from
  the R gate, keeping its adverse-side, underlying-entry and tape guards. A
  full replay (26 sessions, 235 positions) fired it on 13, all
  top_tier_adaptive, at a median -0.72R. Against today's management (time
  stop, peak giveback) it cost -0.43R per firing, CI [-0.85, -0.05], and
  -5.58R in total. Every variant tried was negative too: a 5-20 minute
  grace, a 0.25-0.75R loss cap, no tape confirmation. So
  `use_sr_loss_exit` is `false` in every preset and as the code default; in
  effect it is a tighter stop at about -0.7R. It had been live only for
  zero_dte_etf_options' credit verticals (R on the premium mark), the one
  preset where turning it off changed behaviour.

- **Configured zeros are honoured.** *2026-09-24*
  - The score terms' weights and thresholds were read as
    `float(x or default)`, so a `0` meant the default.
    `config.small_cap_squeeze.yaml`'s three zeroed extension penalties
    (`atr_stretch_penalty`, `bollinger_entry_penalty_outer_band`,
    `entry_penalty_near_extension`) were inert and still docked up to 0.75
    from a stretched entry. They now score 0; no other preset zeroes one of
    these keys.
  - The same fix applies to `min_target_rr`, `min_stop_atr_mult`,
    `divergence_exit_partial_frac` / `_min_age_bars` (a 0 fraction used to
    close half the position) and `divergence_max_age_bars` (strategy_base
    and the dashboard read `or 8`).

- **The peer ladder's structure-fail exit fired on HTF breaks from before
  the entry.** *2026-09-24* — it now needs an HTF CHoCH / BoS whose bar
  closed after the entry. Expect fewer `ladder_structure_fail_*` exits right
  after entries taken against an existing HTF break.

- **The retest stop anchor bypassed the stop floor.** *2026-09-24* — it was
  applied after the `min_stop_atr_mult` clamp and could put a stop 0.05%
  from entry. It is re-clamped now (see Changed, breakout family).

- **The fib-pullback override could lift a same-level block for an
  option.** *2026-09-24* — option signals began carrying the underlying's
  `tech_fib_anchor_*` stamps when their entries moved onto the shared
  stage, and `risk._fib_pullback_override` compared those underlying prices
  with the option's premium. It could only fire on an underlying priced
  near the premium, such as IWM. It now never applies to an option signal,
  which restores the old behaviour.

### Known issues

These were found during the 2026-09-24 change and its 2026-09-25 follow-up
and were deliberately left unchanged. Each one needs a decision.

- **Bracket-mode adoption, found with the 2026-09-25 reconcile follow-up.**
  These were left out of that cut because the fix changes the adoption
  contract that the manager, the gatekeeper and the restore share. Bracket
  mode is off in every preset.
  - `ensure_position_protected` places fresh protection when a tracked
    bracket's stop is dead, but it does not cancel that bracket's live
    target first. A target moved by replace may not be OCO-linked, and it
    then rests beside the fresh one.
  - A partly filled STOP_LIMIT stop is adopted as it rests. Its fills are
    already outside the account quantity, and the manager's later cancel
    books them again.
  - A resize that the broker refuses leaves an oversized stop adopted
    (`qty_mismatch`).
  - At 07:00 with extended hours on, fresh protection is an AM-session
    order, and Schwab rejects STOP orders outside NORMAL. The position is
    left `unprotected`, and the engine owns the stop.
  - A dry run restores a real position again at the next reconcile after
    the paper engine exits it.

- **A dry run and the live bot share one reconcile-metadata file.** Every
  preset points `startup_reconcile_metadata_db_path` at
  `.logs/startup_reconcile_metadata.sqlite`, and `SessionRiskStateStore`
  opens the same file. So switching a live bot to a dry run and back can
  lose the live bot's restore state.
  - A dry run's first successful reconcile replaces every row with its own
    positions.
  - A paper exit deletes a real position's row.
  - A row that tracks a working exit order no longer matches in a dry run.
  - The next live restart then restores basic, or skips a position that
    requires hybrid metadata. In bracket mode it adopts and resizes the
    resting stop to the full position beside a still-working exit order.
  - The fix is a per-mode path (for example a `.dry_run` suffix for both
    stores). It changes where a dry run keeps its session risk tallies, so
    it needs a decision.

- **What the S/R veto read is not logged.** `_sr_lists` records neither
  the pending level nor the S/R ATR in ENTRY_CONTEXT. That is part of why
  the vwap_reclaim blocks (see Changed, 2026-09-25) looked like levels the
  bot never logged. Logging both would let the next dry run be replayed
  faithfully.
  - The pending check is memoryless: a flip confirmed earlier in the
    session goes back to pending on any retest. AVGO 09-23 SHORT, 11
    minutes after its flip, is the one case in the study; a "since the
    last cross" rule would change only that entry.
- **Fix G never judges a trail runner.** A trend entry with no qualifying
  rung trades as a runner with no target, and Fix G is inert for it. That
  includes the COST 2026-04-24 geometry that motivated it. Covering runners
  would be a new block, so it has not been added.
- **`restore_basic` cannot see a working exit order.** It has no saved
  metadata, and the working-order snapshot (`extract_working_orders`)
  carries no quantities. The order therefore counts as foreign and blocks
  entries until someone clears it. In bracket mode the restore also
  protects the full broker quantity beside a working slice.
  `restore_hybrid` handles both (see Fixed).
- **Performance of the lazy 5m context builds (measured 2026-09-25; no
  change needed).** `admit` builds its contexts whichever knobs are on,
  because `emit` stamps their lists. key_levels, htf_pivots and
  trend_continuation gate on a 5m LTF that the engine's 1m pre-warm never
  primes.
  - Test setup: real 09-24 tapes with 6 tradables and 4 peers.
  - A lazy build costs about 7-8 ms per candidate on a 208-bar 5m frame,
    and about 10 ms on key_levels_1m's 1m frame.
  - key_levels builds only for a proposal that reaches `admit`.
  - trend_continuation spends a median 48 ms of a 398 ms `entry_signals`,
    and htf_pivots 43 ms of 556 ms. Most of that is their own
    per-candidate structure and technical builds, which predate 09-24.
  - The bigger cost is the 5m frame enrichment, at about 20 ms per symbol
    per cycle.
  - The work is GIL-bound. `_prime_cycle_context_cache`'s docstring
    assumes the builders release the GIL, but a 4-worker pool was slower
    than serial for both the builders and the enrichment.
  - A pre-warm would only move the cost out of `entry_signals`, and it
    would build for every watchlist symbol instead of only the candidates.
    So none is worth building.
- **Leftovers of the 2026-09-25 fixes.** They are latent under every
  preset.
  - The shared score's HTF context (`_default_htf_context_for_score`) and
    the FVG term still make two builds. The score context passes no FVG
    arguments, so it uses the builder's FVG defaults (4 / 0.05 / 0.0005)
    instead of the configured 3 / 0.06 / 0.0006.
  - Merging those two builds needs a single request builder. Before that,
    check that nothing reads HTF FVGs off top_tier's `htf_ctx`.

### Added

- **A skipped entry decision now records which regime it came from.**
  *2026-09-19* — `decisions.csv` carries a `family` column sourced from
  `entry_gatekeeper._decision_entry_family`, which reads
  `details['entry_family']`. top_tier's success path stamped `regime`; the
  skip path passed no details at all, so `family` was `none` on every skipped
  row — and skips are essentially every row (4,316 decisions, 0 trades, in one
  archived session).

  The cost was concrete: a post-mortem could establish that ~20% of RTH
  decisions (22,231 of 109,840 across 13 sessions) die on `no_fresh_breakout`,
  but not whether that was `trend` (25-bar lookback on the LTF, 3 trades ever)
  or `momentum` (6-bar on the base frame, 0 trades) — the difference between a
  tuning problem and a dead regime.

  The skip branch now stamps `entry_family` (the regime that came closest to
  producing a signal, or `none_qualified` when nothing cleared its score
  threshold — a different failure worth telling apart), plus `regime_score`,
  `regime_score_norm` and `regimes_tried`, which separate "nothing was close"
  from "it missed by 0.1 and the threshold may be wrong".

  `entry_family` is the key the gatekeeper already reads, so this populates the
  EXISTING `family=` log field and CSV column — no log-format, parser or schema
  change.

- **`trend` and `momentum` now arm on qualification and enter on the retest.**
  *2026-09-20* — both trigger on `close > max(high of the previous N bars)` (25
  LTF bars, 6 base-1m bars), so the fill sits at the highest price in 25 or 6
  minutes BY CONSTRUCTION, the stop lands inside the retrace that normally
  follows, and `same_level_block_minutes` then bars re-entry at the level where
  the next leg starts.

  The new `entry_timing` block measured it: a trend fill was followed by a
  retrace covering 85% of the way to its stop, against 21% from an arbitrary
  moment in the same session (+0.641R over baseline, 9 trades). `pullback` was
  +0.213R and `vol_squeeze` -0.024R.

  Qualifying now records the level that was cleared and waits for price to come
  back to it (`_armed_retest_verdict`, `ARMED_RETEST_REGIMES`). The retest
  fires the entry; if none comes inside `armed_retest_max_minutes` (12) it
  enters at market, which is the previous behaviour. **That fallback is
  deliberate** — a strong trend day never offers the retest, and forfeiting
  those setups would deepen the "some days it doesn't trade at all" problem
  rather than fix the entry. `armed_retest_enabled: false` is the A/B.

  Only `trend` and `momentum` arm, decided on the same measurement: `pullback`
  already requires a 25-50% leg retracement before it fires, `range` and
  `vwap_reclaim` enter against the move by design, and `vol_squeeze` showed no
  retrace above baseline at all.

  A `wait` short-circuits BEFORE the build method, and leaves the rest of the
  build queue alone — arming `trend` does not stop `pullback` firing on the
  same symbol in the same cycle.

  **Stop rules are unchanged on a retest entry.** The gain is a fill at a level
  price has already tested and held rather than at a fresh extreme; deriving
  the stop from the retest low would mean bypassing `default_stop_pct` /
  `min_stop_atr_mult`, which are risk floors and a separate decision. The
  retest low is stamped in metadata so that can be settled from data.

  Two things found while building it. `_breakout_reference` was extracted so
  the builder's fresh-breakout check and the armed level come from ONE
  computation — two copies would drift the moment either lookback was retuned
  and the bot would arm on one level and enter against another; the extraction
  is behaviour-neutral and the suite confirms it. And invalidation was
  initially measured against the CURRENT N-bar reference, which walks up as new
  highs print: that moved the invalidation line away from price on the setups
  still working and dragged it behind a rolling-over one. Now anchored to the
  armed level. Caught by the tests, not by review.

  All five knobs are declared in both the shipped preset and the manifest, so
  nothing resolves off a code default. Coverage: `tests/test_armed_retest.py`
  (42).

- **The armed retest is now answerable after the fact.** *2026-09-20* — an
  audit before the first live week found the change shipping blind. Its one
  question is "did the retest path fire, or did everything fall back to a
  market entry", and nothing could answer it: `armed_retest_status` was stamped
  on the Signal, but `EntryGatekeeper.structured_metadata_snapshot` filters
  metadata through an allow-list and `armed_retest_` was not a permitted
  prefix, so all three keys were dropped before `events.jsonl`. `TradeRecord`
  had no field for it either, so `trades.csv` had no column and no aggregate
  could group by it. A week would have produced one blended PnL number that
  reads identically whether good fallback entries carried poor retest entries
  or the reverse.

  Four additions, all observability — no decision logic touched:
  * `armed_retest_` added to the ENTRY_CONTEXT prefix allow-list.
  * `TradeRecord.armed_retest_status` / `.armed_retest_waited_minutes`,
    populated from position metadata at exit. `TRADE_CSV_COLUMNS` derives from
    the dataclass, so both reach `trades.csv` (and the archive copy)
    automatically; the existing schema-drift guard rotates the old file rather
    than writing a malformed one.
  * `per_entry_path` in the report and the EOD log — the same columns as
    `per_regime`, bucketed `retest` / `market_fallback` / `immediate`. The
    fallback IS the pre-change behaviour, so it is the control group and gets
    its own bucket rather than being folded in with non-arming regimes.
  * `entry_timing.by_entry_path`, each path carrying its own baseline. This is
    the mechanism test rather than the outcome one: a retest entry should
    retrace less after the fill than a market fallback, because the retrace
    already happened. Equal numbers mean the feature is not working whatever
    the PnL says.

  Found while writing the smoke test, not by the unit tests: the entry-timing
  log line printed `6670.0%`. `_fmt_pct_opt` formats a FRACTION as a percent
  and these fields are already percentages, so they were multiplied by 100
  twice. Every unit test passed because none asserted on the rendered string,
  which is the only thing an operator reads. There are now two tests over the
  rendered line, one of which simply asserts no percentage in it exceeds 100.

- **The session report measures whether entries are chasing.** *2026-09-20* —
  a new `entry_timing` block in the `SESSION_REPORT` payload and the EOD log:
  for every trade, the deepest retrace in the 15 minutes AFTER the fill,
  expressed as a fraction of that trade's own entry-to-stop distance, split by
  regime.

  That denominator is what makes it readable without a second unit — 0.4 means
  the retrace covered 40% of the way to the stop, so a patient entry down there
  would have been 0.4R better with the same stop, and 1.0 means price reached
  the stop, which is what being stopped out IS.

  The point is the regime split. `trend`, `momentum` and `vol_squeeze` can only
  fill at an N-bar extreme (`close > max(high of the previous N bars)`, 25 bars
  and 6 bars respectively), while `pullback` refuses to fire until price has
  given back 25-50% of the leg (`pullback_require_real_dip`). If the bot is
  entering ahead of the natural pullback rather than on it, those two groups
  separate.

  **The baseline is the whole measurement.** Price dips below any given price
  most of the time, so a raw count of "a better entry existed" measures nothing
  — the mistake the first version of `_gate_attribution` made, which ranked a
  gate with no edge as the costliest one. Each trade is therefore scored
  against arbitrary moments in the SAME symbol's session carrying THAT TRADE'S
  risk distance, which controls for the symbol's volatility and the trade's own
  stop width at once.

  On the archived sessions (old code and the pre-retarget universe, so not a
  verdict on the current bot) the baseline is 0.358R against an observed
  0.666R, and the regimes separate as predicted: `trend` +0.641R over baseline
  across 9 trades, `pullback` +0.213R across 8, and `vol_squeeze` -0.024R
  across 11 — no edge at all, consistent with its 0.25R median MAE, the lowest
  of any regime.

  Same honesty guards as `gate_attribution`: labelled NOT a backtest in the
  payload, the docstring and the README, because it says a better price existed
  and not that the fill was reachable. A trade with a non-finite or
  non-positive `initial_risk_per_unit`, a bad entry price, a missing frame or a
  raising bar lookup counts as unevaluated rather than as zero retrace —
  scoring it zero would read as evidence AGAINST chasing, which is the claim on
  trial. A regime under `min_regime_samples` (5) is kept but marked
  `low_sample` and sorted last; without it a regime with a single archived fill
  topped the table at +7.4R.

  No engine change: `bars_for` was already wired for `post_stop_continuation`.
  Coverage: `tests/test_entry_timing.py` (32).

- **The session archive measures what each gate cost.** *2026-09-20* — a new
  `gate_attribution` block in `manifest.json`: for every SKIPPED decision, the
  net forward price move over 30 minutes toward the side the bot was about to
  take, in ATR, bucketed by skip reason and split by the regime that produced
  it.

  Skip reasons were only ever counts. `no_fresh_breakout` fired 22,231 times
  across 13 sessions — 20.2% of all RTH decisions — and a count says how much
  work a gate did, never whether the work was worth doing. Every gate was added
  in response to a specific loss and none had been measured since.

  Two things make it readable, both learned by running it on real archives
  rather than by design:

  The metric is the NET close-to-close move, not the excursion. The first
  version scored "did price move 1 ATR our way" and ranked the
  highest-volume gate as costliest — but 77% of RANDOM 30-minute windows on
  this universe touch 1 ATR up, because a 1m ATR-14 measures fourteen minutes
  of range and the window is thirty. That gate turned out to have no
  directional edge at all. Net movement has a real baseline; excursion does
  not.

  And the baseline is sampled from the SAME session's bars. A trending day
  lifts every LONG-side number, so only the gap above an arbitrary moment in
  the same session counts as evidence.

  Labelled `NOT a backtest` in the payload, the docstring and the README: price
  movement only, no stop, target, sizing or slippage. Gates below
  `min_samples` (20) stay in `by_reason` but are kept out of the ranking — a
  reason seen once has a "median" of that single observation, and on real data
  those produce the largest numbers in the table purely because nothing
  averages out.

  `_regime_call_outcomes` already asked a version of this question of the same
  files, so its bar loader and forward-excursion maths were extracted into
  shared helpers rather than copied — one place for the tz-aware/naive
  timestamp normalisation to live.

  Coverage: `tests/test_gate_attribution.py`, 22 tests.

- **The EOD session report measures post-stop continuation.** *2026-09-19* —
  a new `post_stop_continuation` aggregate (structured payload + log table)
  answering the one question every other aggregate misses: when a trade was
  stopped out, how far did price then run the trade's way? Every other section
  scores the trade as it was closed, which cannot separate "the stop was
  right" from "we were right and got shaken out".

  Measured in R off `initial_risk_per_unit` so it is comparable across symbols
  and sizes, over a `window_minutes` (default 30) bound after the exit —
  deliberately the same span as `same_level_block_minutes`, so the number
  reads directly as the cost of the re-entry lockout. Reports counts reaching
  1R and 2R, avg/median/max R, and `opportunity_usd`.

  `opportunity_usd` is labelled an UPPER BOUND in both the log line and the
  docstring: it assumes re-entry at the stop price and an exit at the window's
  best tick. It is "the move left on the table", not money the bot would have
  made.

  A trade whose `initial_risk_per_unit` is absent, non-positive or NON-FINITE,
  a missing frame, an infinite bar, or a raising bar lookup all count as
  UNEVALUATED rather than as zero continuation — otherwise a data gap would
  quietly read as "the stop was right" and bias the whole aggregate toward
  vindicating stops. The finiteness checks are not belt-and-braces: NaN
  survives `<= 0` because every comparison against NaN is False, and
  `ensure_ohlcv_frame` drops NaN OHLC but NOT inf — both were found by
  fuzzing this function after it was written, having reached the arithmetic
  and turned `opportunity_usd` into NaN and every R aggregate into inf.

  Bars arrive through a `bars_for(symbol)` callable supplied by the engine, so
  `session_report` stays a pure aggregator over `TradeRecord`s with no
  DataFeed dependency. The engine passes 1m bars regardless of the strategy's
  LTF — the finest resolution gives the truest high/low for the window.

  Validated against the archived sessions (old code, so not a verdict on the
  current bot): of 30 stop exits, 27 evaluable, **16 ran at least 1R the
  trade's way within 30 minutes of being stopped**.

- **The dashboard redirects phones to the mobile layout.** *2026-09-19* — a
  document request from a phone User-Agent gets `302 -> /mobile`; tablets and
  desktops are unchanged, since a tablet has the width for the desktop layout.
  Detection is server-side so there is no flash of desktop content. `?desktop=1`
  forces the desktop layout on a phone and is bookmarkable. The redirect fires
  only on the document fallthrough — `/assets/*`, `/api/*`, `/health`, `/mobile`
  and `/m` all return earlier, so the mobile page still loads its own JS and
  polls `/api/state` from a phone UA — and it is a 302 rather than a 301 because
  the response depends on the device, not the URL.

- **`top_tier_adaptive` can now trade a reversal.** *2026-09-18* — a session
  that flushed and then turned produced zero entries on the recovering side.
  Driving a 3% flush that retraced 87% through the gates bar by bar: of 90 bars
  on the recovery leg, 45 blocked on `index_not_confirmed`, 41 had no regime
  qualify at all, 0 signals — both directions. The stale `day_strength` bias is
  not the cause: `_decide_side` voted the correct side on 75 of 90 bars, and an
  explicit side decision already forces `bias_penalty` to 0.0. Two changes,
  which only work as a pair (gate alone 0 signals, builder alone 2, both 18
  LONG / 15 SHORT).
  - **Leg-anchored confirmation** (`leg_anchored_confirmation`, default
    `false`, `true` in the preset). `_frame_agrees` measured `close > vwap`
    against SESSION VWAP — a whole-day average that after a flush sits far
    above price, so the confirmation only turned true long after the reversal
    was running. Applied to every PEER, the breadth gate inherited the lag: on
    the test tape session VWAP was reclaimed 44 minutes after the low with half
    the move gone, a VWAP anchored at the low after 1 minute. New
    `_leg_anchor_vwap` anchors at today's more recent extreme, guarded by
    `leg_anchor_min_age_bars` (20) and `leg_anchor_min_impulse_pct` (0.005) so
    an ordinary pullback is not read as a reversal, and falls back to session
    VWAP when no leg is established. Measured deltas in confirmed bars: pure
    trend 0, grind-with-pullbacks 0, chop +1, V-reversal LONG +22, inverted-V
    SHORT +22. Default `false` because `SmallCapSqueezeStrategy` subclasses
    this strategy and inherits the method.
  - **`vwap_reclaim` enabled in the preset.** Fixing the gate alone still
    produced 0 signals — the blocker moved to `no_fresh_breakout` on 38 of 90
    bars. Every other surviving regime triggers on a BREAKOUT (trend/momentum
    need a fresh N-bar high, pullback an established aligned trend); a reversal
    is a reclaim, so no builder recognised the shape. Knobs adapted from
    `config.small_cap_squeeze.yaml` where the regime is already tuned;
    `vwap_reclaim_buffer_pct` rescaled 0.0025 -> 0.0015 because it runs through
    `_pct_param` and small caps carry a far larger ADR. Entries land at the
    VWAP reclaim — around the midpoint of the move, not at the low.

### Changed

- **top_tier: entries open at 09:35, and `sr_scalp` is back on -- from
  `orb_end_time`.** *2026-09-23*
  - `entry_windows` starts at 09:35 instead of 09:45. The preset runs with
    `disable_orb_regime: true`, so there is no 09:30-09:45 opening-range
    carve-out and the regime mix is live from 09:30 -- the entry window was
    the only thing holding the first ten minutes back, and 09:45 was left
    over from when the ORB range had to form first. The manifest keeps 09:45
    because its defaults leave ORB on, where 09:30-09:45 is a no-entry zone
    regardless.
  - `disable_sr_scalp_regime: false`. It was switched off on 2026-09-18 while
    its stop floor made it unbuildable; that was fixed on 2026-09-22 (the
    stop now leans on how far the level has actually been pierced), and its
    15 archived trades (-$179.67) all predate the fix.
  - `sr_scalp` now waits for `orb_end_time` whether or not the ORB regime is
    on (`_allowed_regimes`). It skipped the opening window because morning
    chop near recent levels breaks through them -- a property of the tape,
    not of ORB -- but with `disable_orb_regime` the primary window starts at
    09:30 and took sr_scalp with it. The boundary matches the ORB-on path to
    the second (excluded through 10:05:00). Pre-market in extended-hours
    mode is unchanged and identical in both modes.
  - Preset and code only; the manifest is untouched.

- **Config / manifest / README drift pass.** *2026-09-22* — every param the
  code reads checked against its preset and manifest, every config field
  against `config.example.yaml` and the README, and every README defaults
  table against the manifests. No effective value changes: each edited preset
  loads to the same config before and after.
  - **Undeclared params.** `config.small_cap_squeeze.yaml` now declares the 89
    top_tier-engine params it reads, at the values it was already running --
    it inherits the engine, but only its own knobs had ever been declared, so
    any code-default edit silently retuned it. The two `peer_confirmed_*`
    subclass presets declare the 19 inherited `peer_confirmed_key_levels`
    params each; `closing_reversal` / `mean_reversion` declare
    `screener_min_pullback_from_high`. `test_param_declaration_drift.py` now
    covers small_cap_squeeze as well as top_tier.
  - **Dead `runner_target_rr` removed** from the `peer_confirmed_htf_pivots`
    and `peer_confirmed_trend_continuation` manifests, presets, the example
    config and their README tables. Nothing read it: the runner target comes
    from `_adaptive_management_components` and `adaptive_runner_target_rr`.
    `volatility_squeeze_breakout` does read its own and keeps it.
  - **`config.example.yaml`** carries the 28 config fields it was missing
    (slippage/overage risk, daily-loss open risk, bracket orders, escalation,
    IV rank, ...). Three keys sat under the next section's header comment,
    and the peak-giveback comment still quoted the pre-2026-05-27 tiers.
  - **README.** 20 undocumented fields documented, plus a new `events`
    section: the options table still listed `event_blackout_file` /
    `event_blackouts`, keys removed on 2026-09-18. Package-default rows
    corrected where they had drifted from the manifest (vsb `min_rvol` /
    `target_rr` / `runner_target_rr`, closing_reversal `min_rvol`, the
    peer_confirmed `force_flatten` rows, top_tier `min_sr_scalp_score`), and
    the top_tier universe prose, `index_symbols` and `sector_index_map`
    brought up to the 2026-09-18 Tech/AI retarget -- they still described 23
    names across six GICS sectors. top_tier README sections renumbered (two
    were numbered 19).
  - `no_new_entries_after: 13:45` quoted in
    `config.zero_dte_etf_long_options.yaml`. Unquoted, YAML 1.1 reads it as
    the sexagesimal integer 825; `parse_hhmm` converts that back, but the
    value should not depend on it.

- **`vol_squeeze` now runs at midday.** *2026-09-21* — its thesis is
  compression resolving into expansion, and the lunchtime tape IS the
  compression, so it had been excluded from the one window where its setup is
  most common. The midday carve-out predates the regime's reinstatement and was
  written about the regimes that existed then.

  The measurement that prompted it, from the live session: across the day's
  five biggest movers (INTC, META, AMD, QCOM, NFLX — all +3% to +6%), **51% of
  midday skips were "no regime qualified"**, and of the three regimes then
  offered, not one came within half a point of its floor — pullback peaked at
  3.00 against a 3.5 floor, momentum at 3.00 against 4.0, vwap_reclaim at 0.00.
  Ninety minutes a day in which effectively nothing could fire. The morning
  profile is completely different: only 4% of blocks were score-related, the
  rest being downstream gates.

  `trend` and `range` stay out of midday — this is not a reversal of the
  carve-out, only of the part that excluded the regime whose setup midday
  produces.

- **README drift: both READMEs said "six regimes".** *2026-09-20* — there have
  been eight since `vwap_reclaim` and `orb` were added; the strategy README's
  regime-defaults table was also missing `disable_vwap_reclaim_regime` and
  `disable_orb_regime`. Corrected, and the table now carries the armed-retest
  knobs too.

- **`vol_squeeze` re-enabled in the `top_tier_adaptive` preset.** *2026-09-20* —
  the 2026-09-18 regime trim turned it off on the grounds that it "had the
  highest score ceiling (6.5) so it won more auctions than its edge justified".
  That was true of RAW score sorting, and `_normalized_regime_score` +
  `REGIME_SCORE_CEILINGS` shipped in the SAME change — the two are consecutive
  bullets in this file's 2026-09-18 Added section. Ranking is now
  `(score - floor) / (ceiling - floor)`, under which a 6.5 ceiling over a 4.0
  floor is the WIDEST headroom of any enabled regime (trend/momentum 2.0,
  pullback/range/vwap_reclaim 1.5), so the property cited as the reason to
  disable it now holds it back rather than flattering it: a vol_squeeze at 5.5
  ranks level with a pullback at 4.4.

  The second reason is a hand-off that had nowhere to go. `bollinger_squeeze`
  is read in exactly two places in the strategy — this regime's scorer
  (`_score_vol_squeeze`) and `range`'s `reject_range_during_squeeze` rejection
  — and they partition the same condition: `range` declines squeeze setups
  *because* vol_squeeze is meant to take them. With vol_squeeze off, `range`
  kept declining and nothing picked them up. The new `gate_attribution` block
  puts a number on it: on the 2026-09-18 archive
  `long_build_failed_range_bollinger_squeeze` blocked 149 decisions at +0.853
  ATR above the same-session baseline.

  No code change and no conflict with the other regimes: the build queue is
  flat and cross-side (a regime cannot block another, within or across sides),
  and `vol_squeeze` is not referenced in `strategy_base.py`, `engine.py` or
  `risk.py` at all. Three interactions are real but pre-existing and shared by
  every regime — one more competitor for `risk.max_positions` (4) slots, one
  more producer of `same_level_block_minutes` re-entry lockouts (which are
  symbol + side + level scoped, regime-agnostic), and no runner treatment on
  exit (that path is trend/pullback only). The regime is exempt from
  `reject_oversized_entry_bar`, as it has been since 2026-05-14, because a big
  bar IS the setup.

  This is a measurement, not a verdict. The trim's small/mid-cap objection
  still stands and is untested on this universe: with the flag off the regime
  is never scored and is omitted from the `no_qualifying_regime` skip line, so
  there is no record of how often it would have qualified on mega caps. All 12
  `vol_squeeze_*` knobs are already declared explicitly in the preset, so
  nothing resolves off a code default.

- **`enable_vwap_reclaim_regime` -> `disable_vwap_reclaim_regime`.**
  *2026-09-20* — all eight regime knobs now read the same way. The flag shipped
  2026-05-30 as an opt-IN (default off) so that adding the regime to the shared
  engine could not silently switch it on for `SmallCapSqueezeStrategy`, which
  subclasses `TopTierAdaptiveStrategy`. That protection is spent:
  small_cap_squeeze sets the flag explicitly in its own manifest, so nothing
  depended on the inverted default, while the odd polarity left one knob
  reading backwards from the other seven.

  Behaviour is unchanged — both shipped presets ran the regime before and run
  it after, verified by resolving the loaded config for each. One reader
  (`strategy.py`), two yaml presets, one manifest; renamed and inverted in one
  cut with no alias.

  Two absent declarations surfaced while doing it, both cases of a CODE default
  doing load-bearing work: `top_tier_adaptive/manifest.json` declared neither
  `disable_vwap_reclaim_regime` nor `disable_orb_regime`, so a config-less run
  resolved both off `params.get(..., False)` rather than anything declared.
  Both are now declared at their existing effective values, so the defaults are
  inert. Note the baseline that exposes: the manifest says ORB **on** while
  every shipped preset turns it off — worth a separate decision.

  Coverage: `tests/test_regime_flag_polarity.py`, 55 tests. Pins the property
  rather than the instance — a ninth regime added with an `enable_*` knob fails
  across every manifest, every shipped config and the strategy's own param
  reads, instead of being discovered by someone reading a config and getting
  the sense of it backwards.

### Fixed

- **Key levels, channels, trendlines and the dashboard charts: the levels
  review.** *2026-09-23* — one pass over the S/R and HTF builders, the
  technical lines, the session indicator overlay, the HTF feed and the
  dashboard charting. Every fix is pinned by a test that fails with the fix
  reverted (`tests/test_fix_htf_feed.py`, `test_fix_key_levels.py`,
  `test_fix_indicator_overlay.py`, `test_fix_technical_lines.py`,
  `test_fix_dashboard_charting.py`, `test_fix_strategy_consumers.py`).

  **HTF feed**
  - The stored HTF frame holds completed bars only. It was fetched ~10 s into
    each bucket and kept the bar that had just opened as if complete: for a
    whole bucket that near-zero-range bar confirmed pivots, cut ATR ~7% and
    set the breakout flags and HTF structure bias (98.3% of replayed
    refreshes stored one; now 0%).
  - No overnight (20:00-07:00) bar is stored. Schwab's price_history lags a
    day on them, so PDH/PDL, pivots, ATR and the HTF chart depended on which
    nights a fetch happened to hold. The window is applied to the base bars,
    before resampling.
  - Bars coarser than 30 minutes are laid out per session segment
    (`utils.session_bucket_bounds`): buckets start at 04:00, 09:30, the close
    (16:00, 13:00 on early closes) and 20:00, and a segment's last bucket is
    cut short at the next boundary. The 60m grid is 07:00, 08:00, 09:00 (to
    09:30), 09:30 ... 15:30 (to 16:00), 16:00 ... 19:00, as charting
    platforms draw it; 1/5/15/30m bars are unchanged. On one 09:30 grid the
    60m bar labelled 15:30 held 15:30-16:29, so post-market prints set the
    60m PDH/PDL of every peer_confirmed preset and counted as regular-session
    in the session indicators; the first premarket bar was labelled 06:30,
    outside the stream window; and lengths that do not divide an hour
    drifted an hour off the local grid across a DST change. A bar is
    complete when its bucket ends (`utils.session_bucket_ends`), so the
    short 15:30 bar is stored at 16:00, not 16:30, and an incremental
    refetch starts on a bucket boundary (starting mid-bucket, it rebuilt a
    stored bar from half its source bars and replaced the whole one).
  - The HTF refresh gate uses the bars' own boundaries
    (`utils.session_bucket_floor`). A clock floor refetched 60m frames at
    XX:00, half a bar before each regular-session bar closed, so the newest
    60m bar was missing from every context for 30 minutes.
  - Every HTF context is rebuilt when the stored frame changes (a configured-
    FVG context equalled a fresh build on 3.7% of replayed reads; now 100%),
    and when the session date changes, so a dashboard read after midnight no
    longer serves yesterday's prior day/week until the prewarm.
    `fetch_htf_context` is removed.
  - PWH/PWL keep the whole prior week: the trim reaches back to the start of
    the prior W-FRI week, measured from the same session date the builders
    use, so a holiday Monday (Labor Day) no longer cuts it to its Friday.
  - The daily 09:15-09:28 hole in every 1m frame is backfilled, and the
    minute still forming at a history fetch is not stored.
  - `trading_flip_confirmation_1m_bars` / `_5m_bars`: 0 turns that frame's
    gate off (readers turned it back into the default); at least one must be
    above 0 (with both off the adaptive ladder's rung check could never
    confirm); negatives are rejected at load. `config.flip_confirmation_bars`
    is the only reader.

  **Levels**
  - `pending_support` / `pending_resistance` on the S/R and HTF contexts: the
    nearest level price has crossed whose flip is unconfirmed, in its
    original role. Such a level used to be in no list at all until the flip
    confirmed (11-16% of real-bar checkpoints). `nearest_support` stays at or
    below price and `nearest_resistance` at or above it, now also on the
    prior-day fallback path, where a gap morning's PDH came out as a
    resistance below price (8 of 5,712 real-bar builds; now 0): every
    partition, and the broken-level test, is against `close`, and the
    fallback reference price only picks which prior levels stand in for a
    side. A side left empty takes the prior-day/week levels, then the frame
    extreme if those are across price too (with prior levels on, a gap past
    them used to leave the side empty), and a confirmed-lost support between
    price and the old reference is `broken_support` again. A fallback level
    the side already holds is not added a second time (its copies merged
    into doubled touches and score).
  - No cluster cap before the side split: the fresh swing nearest price is no
    longer cut after gaps and trend days (nearest support changes at 12.1% of
    samples, resistance at 7.2%, almost always to a nearer level).
  - Pivots, FVG triplets and order blocks stay inside one ET session. A gap
    spanning the night was in the HTF FVG lists at 29-37% of checkpoints.
    **Policy change to dry-run before live:** bars at a session edge can no
    longer be pivots; nearest S/R moves by more than 0.25 ATR at 12-16% of
    checkpoints and the 0.72-ATR clearance verdict flips at 6-11%.
  - Prior day/week are the regular sessions (09:30-16:00, 13:00 on early
    closes) before the session date (`as_of`, default the clock's), not the
    calendar days before the frame's last bar: a premarket frame served the
    session before yesterday (53 of 53 archived symbol-days in that state),
    and extended-hours prints moved PDH/PDL. `microcap_pm_breakout` keeps the
    prior day's extended-session high as its PMH floor
    (`prior_day_levels(..., regular_session_only=False)`), since an
    after-hours spike is the level its premarket breakout has to clear.
  - FVG `last_seen` is the last bar that traded into the gap, or the bar that
    completed it; it was always the frame's last bar, so recency decay never
    applied. On the 1m LTF the nearest gap's recency is now a median 0.32
    (was ~0.95; 49% at the 0.30 floor), which lowers top_tier's LTF
    `fvg_entry_adjustment` and continuation bias.

  **Session indicators** (`use_rth_session_indicators`)
  - ATR / DI / ADX / RSI / OBV / Bollinger are one session-only series across
    all sessions in the frame, stitched gap-neutral, instead of today's bars
    alone switched on once today had enough bars: no premarket dilution, no
    mid-session step (old steps up to x1.32), no RSI series change at 13:00.
    Returns (`ret1/5/15`) stay all-hours: stitched, they chained today's move
    onto yesterday's close (ret15 flipped sign on 30% of symbol-days at 09:35).
  - The S/R, HTF, FVG and order-block ATR reads the latest session bar while
    the clock is in the session (`utils.latest_atr14`): from 09:30 to 09:44
    the 15m frame's last completed bar is the 09:15 premarket bucket, whose
    all-hours ATR ran a median 0.82x of the session ATR. A premarket reader
    keeps its own bars' ATR.
  - HTF and 1m divergences pair session pivots only; a pair crossing the
    session edge compared two different RSI/OBV series (49.7% of HTF RSI
    divergences).
  - Behaviour change (421 archived symbol-days): 1m atr14 -6.6% at 09:35 and
    -22% at 09:45, -1.5% at 10:30, unchanged from 13:00; 15m S/R ATR +15% at
    09:45. On the span-5 LTF the share of symbol-days with ADX >= 18
    (top_tier's `min_adx14`) goes 22% -> 10% at 09:35, 23% -> 13% at 09:45,
    24% -> 16% at 10:00 and 26% -> 20% at 10:30; the verdict flips on 23-28%
    of symbol-days through 10:44 (15% at 11:30), the +DI/-DI sign on 13-27%,
    and RSI moves a median 2-5 points through 11:30.

  **Trendlines and channels**
  - A crossed support/resistance line pair, or one under half a break buffer
    wide, is dropped; a crossed pair raised both break flags on one bar. A
    close inside both respected bands of a narrow channel sets neither
    respected flag.
  - Trendline and channel windows start at the current session's first bar;
    at the open they counted the overnight gap as one bar and read it as a
    break. Fib and anchored-VWAP impulses skip extended-hours pivots instead
    of being session-bounded, so 5m presets keep them from the open; the
    pivots are filtered before same-kind runs are merged, so a premarket
    extreme no longer takes the session pivot it absorbed down with it. On
    the 5m LTF presets (the peer_confirmed family) trendlines and channels
    need today's pivots and are absent for roughly the first hour.
    Anchored VWAP enabled on its own gets its impulse (base pivots were only
    found when fib, lines, channels or divergence were on).
  - Line and divergence positions index the frame passed in (the dashboard
    drew every line as a zero-length stub).
  - The tolerances and the break buffer lose their own percent floors,
    which decided all 74,033 replayed evaluations; they are multiples of
    `atr_value` = max(ATR14, 0.15% of price), whose floor still decides
    ~70-80% of 1m large-cap bars.
    `trendline_breakout_buffer_atr_mult` defaults to 0.65 (was 0.15, which
    on large caps never applied), reproducing the old 0.10% floor at the
    median on 1m bars; the 5m presets use 0.30. The small-cap presets keep
    0.15 -- small_cap_squeeze and microcap_* (ATR ~2.6% of price, 0.15 x ATR
    beat the floor on 99.9% of their bars) and the $2-$20 screener presets
    momentum_close, mean_reversion, closing_reversal and
    opening_range_breakout (0.15 x ATR at the median on their screener's own
    candidates) -- so it already was their buffer. Every reader takes the configured value as is;
    all three used to turn a configured 0 into 0.15. The exit reader
    (channel break, anchored-VWAP loss) keeps its 0.10%-of-price floor on
    top of it.
  - The channel-break exit fires on a decisive break (the breaking bar used
    to void the channel). The channel edge penalty and alignment bonus apply
    only inside the channel.
  - No 7-pivot cap. A broken line is spent once a close has cleared the
    break buffer after its last touch and a pivot has then formed on its far
    side: it retires instead of re-raising its break for the whole lookback.
    A wick or a dip that closed inside the buffer does not retire it, so the
    decisive close that follows still raises `trendline_break_*`.
  - The near-extension penalty counts an extension just crossed.

  **Dashboard charts**
  - Lines and channels draw in the chart's coordinates over their own span.
    LTF EMAs are the strategy's own (see the `ltf_ema_fast_span` /
    `ltf_ema_slow_span` entry below: knobs of those names used to move only
    the chart).
  - Candle tags get 14 bars of TA-Lib warmup. Payloads carry
    `source_bar_ts`; the client refetches once per new bar, one request at a
    time, and a request that never settles is aborted after
    max(15 s, 5 polls) so the chart keeps refreshing.
  - The HTF chart continues past the stored frame with buckets from the live
    1m bars, from where the last stored bucket ends, the forming one flagged
    `in_progress`. A chart payload for a view whose timeframe has since
    changed is dropped (an HTF payload in flight when the view was reopened
    in LTF switched the chart back).
  - The structure overlay is drawn. Client bars are keyed by time; cached
    bars follow a server renumbering, and bars with no overlap and an older
    numbering are dropped instead of mixed. Trade markers sit on the bar
    holding the fill. FVGs/order blocks are filtered to the visible window
    before the per-direction cap.
  - `sr_row` publishes the strategy's nearest levels unchanged; broken and
    pending levels are their own zones. A pending level beyond the nearest
    one no longer squashes both zones to zero width.
  - The key-level zones' HTF build falls back to `support_resistance.*` for
    every level parameter a strategy does not declare as `htf_*` (it fell
    back to 60 days / 6 levels / 0.35 ATR, a build top_tier never traded on).

  **Strategies**
  - S/R clearance reads the pending level: a short under an unconfirmed-lost
    support or a long over an unconfirmed-broken resistance is blocked (238
    longs and 123 shorts of 4,144 archived samples were waved through). The
    block reasons report the same clearance, in `zero_dte_etf_options` /
    `zero_dte_etf_long_options` too, whose stale copies of the old reasons
    are removed. The breakout/breakdown escapes on the too-close check are
    removed: they required `nearest_*` on the wrong side of price, which the
    builders never report. `breakdown_below_support` /
    `breakout_above_resistance` still block only a side with no level on it
    (unchanged, and nearly never: a level stood on that side for 1,825 of
    1,840 / 2,811 of 2,890 archived flag checkpoints); such a block now says
    so (`htf_breakdown_below_support(...)`) instead of `too_close_to_htf_*`.
    A study of the flags as a full entry gate found no edge: over 26,447
    archived RTH checkpoints (15 sessions), among entries already passing
    the HTF-bias and clearance gates, flagged vs unflagged differed by
    -0.02 R (LONG) and -0.04 R (SHORT) with 95% CIs spanning 0, while the
    gate would have blocked 36% / 45% of them; on the 69 realized top_tier
    trades the flagged ones did better. So it stays off. Only breaks at most
    30 minutes old hinted at an effect (-0.11 R, CI crossing 0), so the S/R
    context now carries `breakout_age_minutes` / `breakdown_age_minutes`
    (minutes since price last traded at the broken level), logged with every
    entry as `sr_breakout_age_minutes` / `sr_breakdown_age_minutes`, to test
    it on future sessions. Nothing blocks on them.
  - The flags no longer switch off the near-support / near-resistance
    terms: the S/R `bias_score` (+/-0.35), the entry proximity bonus and
    penalty, and zero_dte_etf_options' S/R and candle-at-level scores. The
    flags describe a broken level on the far side of price; `near_*` the
    level on price's own side. With a flag set on about half of all
    checkpoints, an unrelated old level had been dropping these terms.
  - sr_scalp builds off the zone price is testing (end-to-end through
    `_finalize_signal`); the S/R proximity score counts a pending level as
    near; peer_confirmed_htf_pivots' battleground and dashboard candidates
    include it.
  - One close-position bound for exit tape confirmation (the per-call
    `close_pos_threshold` was dead). The session archive holds the stored HTF
    frame (`bars/htf_{N}m/`).
- **Chart patterns, their dashboard display, and a second review of the
  09-23 changes.** *2026-09-23* — each fix is pinned by a test that fails with
  it reverted (`tests/test_fix_chart_patterns.py`, plus additions to
  `test_fix_indicator_overlay.py`, `test_fix_technical_lines.py`,
  `test_fix_htf_feed.py`, `test_fix_dashboard_charting.py` and
  `test_regime_call_outcomes.py`).

  **Chart patterns**
  - A reversal's confirming bar must point the pattern's way. The body test
    was abs(close - open) >= 0.28 of the range, so a red bar closing on its
    low confirmed a double/triple bottom or an inverse H&S, and a green bar a
    top: 202 of 904 archived reversal fires (22%), each one feeding the
    opposing-chart entry filter of six strategies (MU 09-22 12:40 blocked
    longs on a green bar's "double top").
  - `_find_pivots` counts a tied extreme as one pivot, at its first bar -- the
    rule `levels_shared.pivot_points` adopted on 09-22. A tie dropped both
    bars, and the alternation merge then swallowed the opposite pivot between
    them (AMZN 09-21 12:56 paired two highs across a plateau into a double
    top); 87 opposing-filter decisions over 09-21/22 change.
  - Detection and the still-valid checks read the current ET session only.
    Before ~09:30 a window reached back into the prior evening: NVDA 09-21
    07:14 fired a bullish flag whose pole was the weekend gap.
  - The memo cache writes and clears an entry and its frame pin under one
    lock. A thread switch between the two clears let another worker's entry
    outlive its pin, so a recycled address could be served a dead frame's
    `_atr_pct` / `_mean_range`.
  - strategy_base's chart / structure / technical context caches keep each
    keyed frame alive for the cycle. peer_confirmed builds a fresh frame copy
    per candidate; a later candidate could take a freed copy's id and, with
    the same length and last bar, be served its context (NFLX got AAPL's once
    in 728 copies).
  - microcap_pm_breakout's 2c/3c confirmation detects its own TA-Lib candle
    set. It intersected that set with the chart-pattern names, which never
    overlap, so the gate never passed and the strategy could not enter.
  - small_cap_squeeze preset comments: chart patterns only add a same-side
    score bonus in this engine, and `use_opposing_chart_filter` is not read
    by it.

  **Dashboard**
  - A 5m LTF chart marks its forming bucket `in_progress` and leaves it
    untagged; it was drawn as complete and tagged off its first minutes (AAPL
    09-22 10:03: CDLHAMMER; complete, a bearish marubozu). Its patterns still
    read the forming bucket, as the strategy does. A payload with a forming
    bucket carries `forming_ends_at`, and the client refetches once that has
    passed: a bucket whose last minutes print nothing brings no newer 1m bar,
    the LTF chart's only other refetch trigger.
  - The HTF chart tags, and reads its patterns and structure overlay from,
    every bucket drawn as complete, including those completed since the
    stored frame's last refresh, which for at least 10 s into each bucket
    were drawn untagged and left out.
  - Re-expanding a panel left in HTF mode drops the HTF view; the LTF chart
    showed its patterns, overlay and EMA spans over mixed 15m/1m bars. The
    poll-driven chart sync takes patterns and overlay from the entry the bars
    came from, not from the state of the previously selected symbol.

  **Other**
  - Divergence compares pivot prices on the scale the session RSI / OBV were
    computed on (`utils.session_price_scale`): 371 of 571 cross-session HTF
    RSI divergences over 10 sessions were the overnight gap.
  - HTF and 1m refreshes are stamped with the time the bars were cut at.
    Stamped after the response, a refresh answered across a boundary claimed
    a bar it did not hold: the HTF 09:45 bar stayed out of every context
    until 10:15, and the 1m minute before the stream's first bar was never
    backfilled.
  - `regime_call_outcomes` reads every reason on a row, not just `primary`:
    an unqualified side is logged first, so a row where the other side
    qualified was dropped. A signal that was built and then blocked by an
    engine gate (`max_positions`, `correlation_concentration`, ...) is a call
    too. 09-22 re-read: 5,624 calls (not 2,613), 50.0% right.
  - data_feed's schwabdev fallback stub is removed: utils imports schwabdev
    unconditionally, so the stub could never run.
  - README: default windows match the manifests (top_tier's manifest opens
    at 09:45, the preset at 09:35; peer_confirmed_key_levels closes entries
    at 15:15, _1m at 15:20), all eight top_tier regimes are listed, and
    `disable_orb_window` spans opening-range end -> `orb_end_time`.
- **`ltf_ema_fast_span` / `ltf_ema_slow_span` set the LTF EMAs the entry
  scoring reads.** *2026-09-23* — top_tier_adaptive and small_cap_squeeze.
  Knobs of these names existed until today, but only the dashboard read them
  (to draw a line), so changing them moved the chart and not the bot; the
  only way to change the EMAs the bot scores on was
  `ltf_indicator_span_scale`, which also stretches ATR, ADX, RSI, Bollinger
  and the returns, and every stop and threshold calibrated on them. They now
  set the ema9 / ema20 columns of the strategy's LTF frame and nothing else
  (`add_indicators(ema_spans=...)`, through `get_merged`, its per-cycle cache
  and `_resampled_frame`); the side-decision EMA vote and the trend /
  pullback / range / vol_squeeze / momentum / vwap_reclaim scores read them,
  and the compact chart and the snapshot bars draw the same lines (also at
  span scale 1, where the chart used to skip its fetch). Declared at their
  current values -- 45 / 100 in top_tier, 9 / 20 in small_cap_squeeze -- so
  nothing trades differently; a bad pair (fast >= slow, not a whole number
  of bars) fails at strategy construction. Once declared,
  `ltf_indicator_span_scale` no longer moves the LTF EMAs. Unchanged: exits,
  the ema9-extension gate, the stretch gate and peer/ETF index confirmation
  read the base 1m frame's native EMA9 / EMA20, as they always have.
- **`htf_ema_fast_span` / `htf_ema_slow_span` drive top_tier, and the HTF
  trend readers agree with what each strategy trades on.** *2026-09-24*
  - top_tier's HTF context hard-coded EMA 50/200 and it read no HTF EMA, so
    the two knobs only drew the HTF chart. The context is now built on them
    (on the strategy's own HTF, `htf_minutes`), and top_tier can trade on
    the HTF EMA trend -- the peer strategies' 2-of-3 vote of close vs the
    fast EMA, fast vs slow, and the context's trend bias:
    `require_htf_ema_alignment` blocks an entry against it
    (`htf_ema_trend_<bias>`) on the sides it names -- `enabled` / `true`
    (both), `long_only`, `short_only`, or `disabled` / `false`; anything
    else fails at construction, naming the key -- and
    `htf_ema_alignment_score` adds (aligned) or subtracts (opposed) that
    much to every scored regime of the side before it has to clear its
    floor. Both follow the ORB-window HTF bypass and ship OFF: over 21
    archived top_tier sessions the trend separated qualified LONG
    checkpoints (+0.28 R aligned vs opposed, CI clear of 0) but not SHORTs
    (-0.14 R, the afternoon -0.50 R) -- hence a mode rather than a bool,
    `long_only` being what that evidence points at -- and realized trades
    ran the other way in every variant; the entries a gate would block were
    mostly the reversal (vwap_reclaim / range) entries. Entry and trade logs
    carry `htf_ema_trend` / `_votes` / `_bonus` for a dry-run A/B.
  - The scoring HTF context (`_default_htf_context_for_score`: top_tier's,
    and the HTF divergence score of the strategies that take the
    support_resistance defaults) is built on the strategy's own HTF
    (`htf_minutes` / `htf_lookback_days`), the frame the engine refreshes,
    not the support_resistance timeframe. For every strategy but one the
    two are the same frame; peer_confirmed_htf_pivots (60m) asked for a 15m
    frame nothing stored, so its HTF RSI divergence score
    (`shared_entry.use_htf_divergence_filter`: +0.20 aligned, -0.25 counter,
    +0.10 hidden) was 0 on every cycle. It now reads the 60m family context
    its peer votes use and its prefetch warms, adds the adjustment to
    `final_priority_score` -- which ranks its signals (side, then slot) and
    gates nothing -- and records it as `htf_divergence_adjustment`, which the
    entry and trade logs keep (`htf_divergence_` prefix).
  - The dashboard's HTF FVGs and RSI divergence lines come from the
    strategy's own HTF level build (`dashboard_level_context_spec`, the one
    its level zones use), not a support_resistance build with EMA 50/200.
    For the peer family that is the very context its divergence score reads
    (one cache entry), so a preset changing `htf_pivot_span` no longer
    charts divergences the score does not apply.
  - One resolver (`utils.htf_ema_spans`, default 50/200) for every HTF EMA
    consumer: the strategies' HTF contexts, the peer prefetch, the
    dashboard's HTF chart and level zones (they fell back to 50/200, 34/200
    and 9/20). A bad pair fails at strategy construction, naming the key; it
    used to be swallowed by the HTF context builder -- no HTF context, so no
    EMA gate and no HTF divergence -- without a word. small_cap_squeeze
    declares the spans it reads.
  - The dashboard sidebar's HTF trend is the strategy's own read
    (`dashboard_htf_trend`): the peer family's EMA vote, top_tier's, and
    zero_dte's `summarize_htf_trend`. It was always a 50/200 context of its
    own, so on a peer preset (34/200) it could say "Bullish" while the
    strategy's HTF gate read neutral and blocked the long.
    peer_confirmed_htf_pivots, which trades on no HTF EMA trend of its own,
    keeps the generic read.
  - The HTF chart draws the EMAs the strategy reads: zero_dte's continuous
    `ema9_all` / `ema20_all` (it drew the session-reset ema9 / ema20 its gate
    never reads, different on every RTH bar), declared spans also at 9/20
    (skipped before), and blank where the stored frame is too short for the
    bot to compute them (it drew an EMA200 the bot did not have).
  - A level-zone spec error is logged and gives no zones, instead of a
    generic 60m / 60-day build that fetched from Schwab each hour.
  - peer_confirmed_trend_continuation's `directional_vote_edge` is the net
    HTF vote FOR the signal's side, as key_levels ranks (it was the absolute
    difference: with no HTF gate there, a short against a 3v0 bullish HTF
    took the maximum ranking credit); htf_pivots' too (the same on every
    signal its peer gate passes).
  - The peer strategies' and zero_dte's prefetch warm the HTF context their
    entry path reads (the peer prefetch left out the FVG arguments, zero_dte
    passed only the timeframe, so every prefetched context was a separate
    cache entry no decision read).
  - `require_htf_bias_alignment` is no longer declared by the
    trend_continuation and htf_pivots presets: only key_levels reads it.
- **Final bug pass over the 09-23 batch.** *2026-09-23* — seven reviewers (the
  data layer, level builders, strategy consumers, dashboard, chart patterns
  and report, config/docs, and a runtime replay of this tree against the last
  commit on archived sessions), every finding reproduced independently before
  it counted. Each code fix is pinned by a test that fails with it reverted.
  - The crossed-trendline guard dropped every pair within 3 break buffers.
    Sized at the old 0.15 ATR buffer, at 0.65 that is 1.95 ATR: 29% of the
    replayed pairs lost both lines (stop anchor, target cap, break exit,
    respected bonuses), almost none of them crossed (2.7%) and some of them
    the very channel the channel builder accepted. Only a crossed pair or
    one under half a buffer wide is dropped now; a close inside both
    respected bands of a narrower channel sets neither respected flag.
  - microcap_pm_breakout: a retest the FVG / order-block plan admitted below
    the PMH kept the PMH as its stop floor, which the anchor helper then
    clamped to 0.05% under the fill (DXST 06-02 10:24, XOS 06-03 08:26: a
    full-size position stopped out on the next bar, above the structural
    stop). It now takes max(anchor, entry x (1 - default_stop_pct)), and is
    refused with no anchor below the entry.
  - The four $2-$20 screener presets (momentum_close, mean_reversion,
    closing_reversal, opening_range_breakout) keep the 0.15 trendline
    buffer the small-cap presets keep: they had been given the large-cap
    0.65, 3-4x the move on their screener's own candidates. Their
    `bollinger_squeeze_width_pct` is 0.011, the p25 on those candidates:
    the large-cap 0.0025 they carried since 09-22 was never reached, so the
    squeeze never fired (momentum_close has the bands off; its value is
    inert).
  - With `divergence_rsi_length` other than 14 the divergence RSI was built
    on raw all-hours closes while the pivot prices are compared on the
    gap-free session scale; it is now built on that scale too. No preset
    sets another length.
  - The session report's gate attribution scores every gate on a row, not
    just `primary`: behind the other side's unqualified reason, the gate
    that stopped a qualified build was never scored, and a signal an engine
    gate blocked was filed under the signal's name (`max_positions` and
    `correlation_concentration` never appeared). Reasons are split at the
    commas outside their parentheses.
  - The 5m LTF chart payload reads the 1m frame before the 5m one, so
    `source_bar_ts` can never name a minute its plotted bucket lacks.
  - `options.min_underlying_price` is enforced before the chain is fetched
    (`underlying_below_min_price`); it was documented and set in every
    options preset but read by nothing. SPY and QQQ are far above it.
  - Docs: tolerances and the break buffer multiply `atr_value` = max(ATR14,
    0.15% of price), whose floor decides ~70-80% of 1m large-cap bars (not
    "pure ATR multiples"); the unused divergence-entry knobs are marked as
    such; the overnight-gap note (the HTF chart holds no overnight bars);
    the session-archive layout; top_tier's regime lists and index gate
    (vwap_reclaim); small_cap_squeeze's regime set and its ladder, which
    never scales out; README_STRATEGY_START_TIMES rows now match the presets;
    the tied-pivot test now has the low-before-the-top shape that actually
    lost a low.
- **top_tier: an expired armed retest took its market fallback only by
  coincidence.** *2026-09-23* — at expiry the fallback re-ran the builder's
  fresh-N-bar-extreme check, which asks whether THIS bar is a new extreme. A
  runaway that never retested -- the case the fallback exists for -- is rarely
  at one on the exact expiry minute: on 2026-09-22 all three arms that reached
  expiry (CRM, ADBE, NFLX shorts, `touched=0`, still through their levels)
  died on `no_fresh_breakdown` while the moves carried on. The fallback now
  enters while the breakout still HOLDS (close on the trade's side of the
  armed level, the retest's own `reclaimed` test) and drops a faded arm as
  `armed_retest_faded(level=...,close=...)`. Affects both sides.
- **Gate attribution read SHORT gates against the wrong baseline, from the
  wrong bars, on the wrong side.** *2026-09-23* — three defects in the
  manifest's `gate_attribution`, which had flagged the short-side gates as
  costly on 2026-09-22:
  - every gate was compared with the LONG baseline, so on an up day a SHORT
    gate's edge was understated by twice the drift (overstated on a down day);
    each is now compared with its own side's, and `median_edge_atr` carries it;
  - the baseline sampled the whole archived 1m frame -- the prior session and
    extended hours too, about 43% of a top_tier sample, with tiny
    extended-hours ATRs inflating every move -- and is now bounded to the span
    the decisions cover;
  - the side was taken from `side_pref`, the candidate's screener bias, before
    the side the reason names; they disagree on about a quarter of rows
    (2,065 `short_...` rows carried `side_pref=LONG` on 09-22). The reason's
    side now wins.
  Re-measured across all 15 archived top_tier sessions, the short-side
  confirmation-bar and index gates are NOT costly: blocked shorts were
  followed by adverse moves on most days (trend confirmation bar positive in 3
  of 8 sessions, block-weighted -0.23 ATR; trend index confirmation 2 of 10,
  -0.15 ATR). 09-22 was an outlier; no gate was retuned.
- **`regime_call_outcomes` was always 0 for top_tier.** *2026-09-23* — it read
  only `ambiguous_regime` skips, which only the 0DTE options strategy emits,
  so every top_tier manifest reported "0 calls". It now also counts a regime
  that QUALIFIED on a side (its build failing on a later gate, or entering),
  and adds `by_regime`, `by_side`, `sources` and a same-span `baseline`.
  09-22 re-read: 2,613 calls, 50.3% right.
- **Concurrent callers each refreshed the Schwab access token.** *2026-09-23*
  -- schwabdev checks expiry outside its lock and reads the last-known issue
  time inside it, so every thread queued behind the first refresh refreshed
  again (09-22 09:15: four refreshes in two seconds from the prewarm
  fan-out). `call_schwab_client` now checks the token one caller at a time,
  so the first refreshes and the rest find it fresh.

- **Whole-project sweep: order handling, options, data, reports and
  screeners.** *2026-09-22* — every `.py` outside the eight level/pattern
  modules reviewed earlier today. Each fix fails its tests when reverted
  (60-mutation matrix, all caught); new coverage in
  `tests/test_sweep_fixes.py` (87 tests) plus the rewritten resample /
  completion tests in `test_properties.py` and `test_bug_regressions.py`.

  *Live orders*
  - **Sub-penny limits.** Entry/exit limits were the touch plus a spread-scaled
    buffer rounded to 4dp, and the reprice loop multiplies that buffer by
    1.33 / 1.66, so every live reprice on a stock at/above $1 was sub-penny
    (150.1599) and rejected under Rule 612 -- dry-run has no tick check.
    Limits now round to the tick away from the touch (a buy up, a sell down).
  - **A second exit for the same shares.** A MARKET exit that did not fill in
    its poll window (a halt, an LULD pause) was left working and nothing
    tracked it; the next cycle sent another, and both filled on the resume.
    `OrderResult.may_still_be_working` now marks every order that reached the
    broker without a confirmed terminal state, and the position manager
    tracks it (`working_exit_order`), books its fills from the order's own
    record each cycle, re-cancels a live limit, and sends nothing else for
    the position until it is terminal.
  - **A partial fill read as "cancelled".** The post-cancel check returned
    success for any order with fills, so a partially filled order whose
    remainder was still live was treated as done. It now waits for a
    terminal status.
  - **Recovery read a stale positions snapshot.** Order-uncertainty recovery
    read broker positions through a snapshot cached once per cycle; every
    recovery after the cycle's first read positions from before its own
    order (a filled exit looked unfilled, a filled entry looked absent), and
    a failed read was taken as "no position" and booked a full exit. Entries
    and exits are now settled from the order's own fills, the snapshot
    (`broker_position_row(s)`, `begin_cycle`/`end_cycle`) is gone, and a
    filled entry whose fill no longer fits the signal's levels is tracked
    with fallback levels instead of left untracked.
  - **A filled exit with no price was re-sent.** With neither a broker fill
    price nor a last price the booking was skipped "to retry next cycle" --
    for shares already sold. It is booked at entry, flagged estimated.
  - **Vertical fill prices averaged the legs.** A 2.00/1.00 debit spread read
    back as 1.50; the net is now the signed sum of the leg prices. Its
    filled QUANTITY had the same flaw where an order carries no
    `filledQuantity`: the executions arrive once per leg, so summing them
    read a 2-lot vertical as 4 filled. It is now the least-filled leg per
    unit of order quantity.
  - **Messages that needed a recheck and did not get one:**
    `partial_fill_*` and `bracket_*` failures now count as having reached
    the broker.

  *Broker-side brackets (off by default)*
  - **Replace kept the old id.** Schwab's replace cancels the child and
    creates a new order; the bracket kept the dead id, so later replaces
    were rejected (the broker stop froze while the engine deferred to it)
    and the replacement's fill was never booked. The bracket now follows the
    new id.
  - **Cancel before an engine exit ignored child fills**, and cancelled only
    the OCO wrapper (a replaced child may not be under it). It now takes
    down every tracked child and reports what filled first; the engine books
    that and exits only the rest. After a child fills, anything left of the
    bracket is cancelled.
  - **Adoption never resized** (a dead key comparison), recorded the engine's
    levels instead of the broker's, and on a restart adopted off the parent's
    original children -- REPLACED after any move -- then stacked fresh
    protection on the live replacement. It now compares the resting quantity,
    records what the broker holds, and adopts the ids the bot last tracked
    (or, for restore_basic, the stop found resting at startup).
  - **Own stops blocked all entries after a restart.** A restored position's
    protective orders counted as foreign working orders
    (`working_orders_present` for the whole session).

  *Session state*
  - **Positions closed overnight stayed tracked.** The new-day reconcile only
    added positions; one closed in the Schwab app kept a slot, fed the
    correlation guard and open risk, and sent rejected exits every cycle. It
    is now booked as `closed_outside_bot` at the last mark (estimated,
    broker-recovered), dropped or cut to what the broker holds, and its
    resting bracket cancelled. Skipped in dry-run, where positions are
    simulated and the real account holding none of them is not a close --
    the first cut wiped every paper position held into a new session.
  - **1m frames grew without bound** for always-active symbols; frames now
    keep the latest session day whole plus the depth of the deepest history
    fetch (the deepest, so one short response cannot trim good history).
  - **Dec 31 was a holiday** when New Year's Day falls on a Saturday (NYSE
    stays open; next 2027-12-31).

  *Options*
  - **Exits compared the underlying with option premium.** R, the time stop,
    the S/R-break guards and the anchored-VWAP arming read the underlying's
    frame against premium-space entry/extremes: a debit position read as
    about +7R (discretionary exits always armed), a credit spread about -5R
    (never), and the time stop could never fire. R now uses the option's
    mark; the rest use `underlying_entry` and the underlying's range since
    entry, both tracked by the position manager. Option entries also record
    their opening stop, so R no longer re-bases after a ratchet.
  - **`natural` / `bid` vertical pricing ignored direction** -- `natural`
    took the ask for a credit open and a debit close (the far side). Both
    modes now follow whether the order buys or sells the spread.

  *Data and reports*
  - **Resampled bars were one source bar late, and end-labelled.**
    `resample_bars` ran `closed="right", label="right"` on start-labelled
    bars: the 5m bar labelled 09:35 held 09:31-09:35 and the 09:30 bar mixed
    four premarket minutes into the open. And since everything downstream
    reads a timestamp as a bar START (the RTH mask behind session VWAP/EMA,
    session-open helpers, the ORB follow-through gate), an end-labelled
    premarket bar counted as RTH. Bars are now labelled at their START `T`,
    the broker's own convention (clock bars for 5/15/30m; coarser bars
    follow the session grid in the levels-review entry above, whose last
    bucket in a segment is short), and a bar is complete when its bucket
    ends (`utils.session_bucket_ends`). The `time_label` /
    `source_bar_minutes` attrs are gone.
    This moves top_tier's 5m LTF structure bars by one minute.
  - **Partial exits vanished from every report.** The EOD report,
    `trades.csv`, the manifest and the account snapshot kept only final
    exit slices (100 shares out 40 + 60 at +$1 reported $60). Slices now
    fold into one row per trade.
  - **Fractional score thresholds were rounded up** (`min_ltf_score` 2.5 -> 3,
    `min_total_score` 5.5 -> 6) in the peer_confirmed presets.

  *Screeners*
  - **ARM and TSM never reached top_tier** (0 of 526 candidate cycles on
    2026-09-21): the curated list ran through the open-screen filters --
    ADRs are TradingView type `dr`, and TSM is not a primary listing. The
    same filters ran on the peer_confirmed lists. Curated lists now filter
    only to their names, off OTC; top_tier logs any configured name the
    screen does not return.
  - **The library's default universe dropped every ETF.** Its `filter2`
    survives `where()`, so pairs_residual's QQQ reference never came back
    (and it is the "0 rows for ETFs" the 0DTE screener routed around on
    2026-05-19). Each screen now states its own instrument conditions.
  - **The description filters were no-ops.** `not_like("%ETF%")` and nine
    siblings matched nothing -- TradingView takes the `%` literally (checked
    live) -- so 56 preferred shares passed the open screens; without the `%`
    "Unit" would drop UnitedHealth, United Airlines, UPS and URI. Replaced by
    `typespecs has_none_of ["preferred"]`.

  *Strategies*
  - **top_tier: an expired armed retest died on a side vote gone
    undecided.** The expiry fallback is exempt from the per-cycle gates
    (they were all satisfied at arm time -- the INTC 2026-09-21 fix), but
    the `side_undecided` gate lacked the exemption, so a vote flickering to
    undecided while the arm waited dropped the entry silently.
  - **microcap_pm_breakout: the blowoff guard measured R off the trailing
    stop.** Once the stop reached breakeven, R divided by ~1e-8 and
    `blowoff_min_rr` cleared on any wide bar at +0.1R. It now uses the
    opening stop, like every other R in the exit path.

  *Levels*
  - **A swing whose extreme printed twice had no pivot.** The shared
    `pivot_points` disqualified every bar of a window holding a tie, so a
    flat top or a two-bar bottom registered nothing -- 4.4% of swing
    highs/lows on archived 5m RTH bars (180 of ~4,130 over 138
    symbol-days), exact penny ties that are routine at round numbers, and
    more on 1m bars (the SPY 1m snapshot gains 50 structure pivots, 282 ->
    332). A tied V-bottom lost its low entirely and market structure read an
    older pivot as the reference low -- surfaced when the corrected 5m
    buckets put a tied top_tier reversal bottom in two bars. A tied extreme
    is now one pivot, at its first bar. S/R, HTF and technical-level
    snapshots regenerated (touch counts up, level prices move by cents).
  - **top_tier: the trailing-bias memory counted loop cycles, not bars.**
    One observation was appended per `entry_signals` call, so
    `trailing_bias_lookback: 10` spanned 20-30 seconds at a 2s loop
    (varying with cycle time), and the memory written for the GOOG
    2026-04-23 case -- a LONG into ten SHORT-biased bars -- had faded long
    before it mattered. It now keeps one observation per LTF bar (a later
    cycle on the same bar updates it) and starts empty each session. The
    penalty it feeds scales with the day's move, so inside the neutral band
    it stays small.

- **`bollinger_squeeze_width_pct` was a constant in every preset but
  top_tier.** *2026-09-22* — the same units error top_tier's value was
  corrected for earlier today: `bollinger_width_pct` is `(bb_upper - bb_lower)
  / bb_mid`, a FRACTION of price, and `0.06` sits above nearly every bar the
  bot computes it on, so `bollinger_squeeze` was simply on. Each preset now
  carries the p25 of RTH BB(20,2) widths on the timeframe that strategy
  actually builds its technical context on, over its own universe's archived
  bars:

      universe / timeframe      value    presets                                   0.06 flagged -> now
      large caps, 1m            0.0025   top_tier + 7 other 1m large-cap presets,   99.9% -> 25.0%
                                         peer_confirmed_key_levels_1m
      large caps, 5m LTF        0.0061   peer_confirmed_htf_pivots /                98.4% -> 25.0%
                                         _trend_continuation / _key_levels
      SPY / QQQ, 1m             0.0014   both zero_dte presets                     100.0% -> 26.9%
      small caps, 1m            0.076    small_cap_squeeze, both microcap presets   16.4% -> 25.1%

  Large-cap values use the pooled definition top_tier's `0.0025` was set with,
  which includes the sector ETFs and indices in the archive; on candidate
  stocks alone `0.0025` flags 19.6% of bars and their own p25 is `0.0028`.
  `peer_confirmed_key_levels` builds its trading context on 1m but only reads
  Bollinger REJECT flags there; the dashboard shows its squeeze flag on the 5m
  LTF, so it is calibrated on 5m. Six presets have no trading path that reads
  the flag (microcap_gap_orb, both zero_dte, peer_confirmed_key_levels, `_1m`,
  and peer_confirmed_htf_pivots, whose only use is the target cap it
  disables); they are set for consistency and say so.

  **Small caps were not broken.** Their 1m bands are wide (median width 0.117
  of price), so `0.06` flagged 16.4% of bars, a working threshold. They move
  to their p25, `0.076`, so the flag means the tightest quarter everywhere, on
  thin evidence: 1,646 bars from 9 symbol-days of dry-run momentum names,
  per-symbol-day p25 0.043-0.135.

  **`volatility_squeeze_breakout` barely moves.** Expected to lose candidates
  sharply once the flag stopped being constant; it does not. The flag is one
  of three alternatives inside its `no_valid_squeeze` gate, and replaying the
  real `entry_signals` over the same 17 sessions (29,916 evaluations) takes
  that gate from 18,367 rejections to 18,368 and
  `bollinger_squeeze_not_confirmed` from 22 to 97. It produced zero signals at
  either value: `weak_day_strength` alone rejects ~99%. Its other gates were
  deliberately not touched.

  Where the flag is live it now also stops suppressing two shared terms most
  of the time: the Bollinger target cap (only closing_reversal and
  mean_reversion enable `target_use_bollinger`) and the weak-ADX entry penalty
  in `_technical_entry_adjustment`, a priority/score input.
  `configs/config.yaml` was left at `0.06` on purpose (runtime config), as was
  the code default, which only fits small caps.

- **Ten defects in the level and pattern modules.** *2026-09-22* — from a
  review of sr_ladder, chart_patterns, candles, htf_levels, levels_shared,
  order_blocks, support_resistance and technical_levels. Each was reproduced
  before it was fixed; the numbers below are from replaying the archived
  2026-09-21 session (28 symbols, real 1m bars) before and after.

  **`anchored_vwap_open` turned into a rolling VWAP by late morning.** It was
  computed after `build_technical_levels_context` had cut the frame to its
  longest lookback (~120 bars), so once 09:30 left that window the session
  anchor silently became the window's first bar. Against the archived
  `vwap_rth` it was 0.00 ATR off at 11:15, 3.4 ATR (median) at 12:00 and 6.1
  at 13:30, with a 20.9 ATR worst case. It drives `anchored_vwap_loss_exit`.
  It is now computed on the untrimmed frame: 0.000 ATR at every checkpoint
  from 10:30 to 15:00.

  **`atr_expansion_mult` measured displacement, not volatility.** It was
  `|close - close[-6]| / atr14`, so a clean trend read as an ATR expansion and
  a violent chop with no net move read as none. Every consumer documents it as
  volatility against the ATR the stops were sized on, and it is now exactly
  that: the mean true range of the last `atr_expansion_lookback` bars over the
  `atr14` just before them. The literal reading of the docs, `atr14` over its
  own 5-bar mean, was measured and rejected: ATR14 is smoothed so heavily that
  the ratio sits at 1.00 ± 0.06. How often each consumer's threshold is met
  (9,498 RTH bars; the second pair is the 453 bars that close out of a <= 2.8
  ATR 12-bar box):

      threshold  consumer                                all bars      squeeze breakouts
                                                         old    new      old    new
      >= 0.80    shared entry-score ATR bonus            54%    70%      90%    73%
      >= 1.00    volatility_squeeze_breakout gate        46%    37%      83%    37%
      >= 1.12    volatility_squeeze_breakout runner      40%    23%      78%    20%
      >= 1.20    top_tier Tier 2a stop widening          37%    16%      75%    13%
      >= 1.25    volatility_squeeze_breakout quality     35%    13%      72%    10%

  Tier 2a's full widening (1.8x in top_tier, reached at twice the threshold)
  now applies on 0.6% of bars, down from 8.8%. **`volatility_squeeze_breakout`
  was NOT retuned.** Its `min_atr_expansion_mult: 1.0` gate was, in effect,
  tuned against displacement, which is naturally large on a breakout, and it
  now passes 37% of squeeze breakouts instead of 83%. That preset is not in
  the live rotation; retune it against outcomes before running it again.

  **A right-labelled HTF bar was treated as complete while still forming.**
  `resample_bars` labels with `closed="right"`, so the bar labelled T includes
  the source bar STARTING at T, and it is complete at T + one source bar, not
  at T. `_completed_htf_frame` admitted it at T. At 11:10, the "completed" 60m
  frame built from 30m bars carried an 11:00 bar whose 11:00-11:30 half was
  still forming, and HTF data is fetched once per bar, so that partial bar was
  used for the rest of the hour. `resample_bars` now records
  `source_bar_minutes` in `attrs`, the HTF cache carries it through its
  merges, and completion waits for the last source bar. A frame without it
  waits a whole HTF bar: late, never early.

  **One structure-event window was sizing two timeframes.**
  `structure_event_lookback_bars` judged both LTF structure events and the S/R
  context's HTF ones. top_tier halved it 8 -> 4 for 5m structure bars on
  2026-05-27, and that silently cut the 15m HTF window from 120 minutes to 60.
  The new `support_resistance.htf_structure_event_lookback_bars` (`null` =
  same as the LTF knob) sizes the HTF window. top_tier_adaptive and
  small_cap_squeeze pin it at 8, and every other preset resolves as before.
  The builders, the dashboard's HTF overlay and every age check that reads the
  S/R context's `market_structure` now use it. That means
  peer_confirmed_key_levels' ladder exits and zero_dte_etf_options' HTF
  structure score (6/6 there, so its value does not change). A test scans the
  whole package for any age check on that structure without `htf=True`. The
  first version of this fix missed zero_dte's four checks, and the scan is
  what found them.

  **Divergence paired the newest swing with the OLDEST qualifying one.** Lows
  at 100 (RSI 20), 96 (RSI 35) and 95 (RSI 30) were reported as a bullish
  divergence 100 -> 95. That stepped over the 96 swing, where RSI had in fact
  CONFIRMED the new low (30 < 35). `find_divergence` now takes the latest
  pivot and the nearest earlier one that makes the pattern's price move. Noise
  pivots inside `price_move_frac` are skipped. An intervening pivot on the
  wrong side of the latest one means there is no divergence to report: for
  regular divergence the latest pivot is then not the extreme, and for hidden
  divergence it broke the swing it claims to hold above (or below). The first
  cut applied that rule to regular divergence only, so lows 95 -> 103 -> 101
  still read as hidden bullish 95 -> 101 across the 103 swing; a bug check
  over the day's changes caught it before commit. One unit test had asserted
  the old pairing (lows 100 -> 99 -> 99.5
  reported as divergence 100 -> 99.5). It now asserts none. The span-scale
  test moved from seed 39 to 309, because seed 39's "divergence" was exactly
  that bogus pairing.

  **`CDLGRAVESTONEDOJI` could never match.** It sits in the bearish list, and
  TA-Lib emits it as +100 (9,181 times across 400 real frames, never
  negative), while bearish matching required a negative value. Fixed-direction
  patterns now match on any non-zero value, and sign-dependent ones still read
  the sign. Every other fixed pattern was checked and is already signed to
  match its list. The gravestone now appears on 1.15% of RTH bars.

  **Chart-pattern thresholds were sized for small caps.** Each
  percent-of-price constant was paired with an ATR-relative one, and the pairs
  coincide at a ~0.67% mean 1m bar range. On liquid mega caps (~0.1%) the
  percent floors were ~7x too wide. The module fired 7 times in 1,850
  evaluations, and 16 of 18 patterns never fired. Those constants now scale by
  the frame's range over that reference, clamped to [0.1, 1.0], so a name at
  or above it keeps exactly the thresholds it had: 97 fires across 10
  patterns, and none above 1.35% of evaluations. The six strategies that gate
  entries on an opposing pattern (closing_reversal, mean_reversion,
  momentum_close, opening_range_breakout, rth_trend_pullback,
  volatility_squeeze_breakout) will now block on patterns they almost never
  saw; top_tier does not use that filter. The flag's slope allowance also had
  an absolute $0.04/bar floor, which let a steadily rising consolidation pass
  as a flag on a $5 name and nothing on a $500 one. It is now 0.1 bar ranges,
  which gives the same verdict on the same shape at any price.

  **Order blocks were born filled.** Fill was measured from the closes after
  the OB candle, which included the bars between it and the breakout, and
  those are the zone forming, not price returning to it. One block was "64%
  filled" at birth. Fill is now measured from the closes after price first
  closes beyond the zone following the OB candle, or after the breakout if it
  has not yet. The first cut measured from whichever breakout found the
  candle, but the same candle is found again from every new extreme within 5
  bars: a breakout, a gap-down close below the zone, then a second breakout
  returned the zone fresh at 0% filled, a block the pre-review code had
  correctly dropped. The same bug check caught it before commit.

  **A trendline price had cut through still counted.** Touches were counted
  and nothing else was checked, so a "support" carried a pivot low 11 points
  below it mid-span. A pivot beyond the line by more than the tolerance
  between its first point and its last touch now disqualifies it. A break
  after the last touch does not; `trendline_break_*` reports that.

  Also: `technical_levels._pivot_points` had become a copy of the shared
  `pivot_points`, and it is now a thin wrapper around it. The HTF FVG detector
  replaced a NaN high/low with 0.0, which would have made a bullish gap
  reaching from zero up to price. The NaN now fails its comparison.

  **Net effect on top_tier, 2026-09-21.** The real `entry_signals` was driven
  over every 5-minute cycle twice, once with every fix above reverted and once
  as shipped. A code fingerprint in each arm confirmed which code had loaded.
  Both arms produced the same 9 entries (same symbols, times, sides and
  regimes) and the same 4,031 skips. 6 of the 9 stops moved, all through Tier
  2a and in both directions: GOOG's `vwap_reclaim` long went 348.73 -> 345.41,
  and AMZN's short 259.91 -> 259.75. The HTF fixes cannot show in this replay,
  because it runs without a data feed. The AVWAP fix acts on exits
  (`anchored_vwap_loss_exit`), not entries.

  Coverage: 1,851 passing (+40). `test_levels_review_fixes.py` holds one class
  per defect, plus divergence cases in `test_levels_shared_divergence.py` and
  O3-O5 in `test_properties.py`. Each fix was reverted in turn, and 15 of 15
  reverts are caught; the two follow-ups were reverted both to their first
  cut and, for order blocks, to the pre-review window, and all are caught. O5
  initially wasn't: `pd.concat` keeps `attrs` only when
  all inputs match, so it now merges a fetch without a source size into a
  cached frame that has one. The three technical-levels snapshots were
  regenerated. Only `anchored_vwap_open` and `atr_expansion_mult` changed, and
  both were checked against an independent computation first.

- **`range` had lost its only counter-trend filter.** *2026-09-22* — found
  on a bug pass over this week's mean-reversion work, and caused by it:
  nothing here was new code, but making `range` able to qualify turned a
  latent interaction into a live one.

  `range` is exempt from the `_decide_side` vote because a fade has to enter
  against the move (2026-09-18). Its counter-trend protection is therefore
  the soft `_bias_penalty` — which is skipped whenever the vote picks a side,
  on the grounds that "the explicit decision already chose the side"
  (2026-05-27). That reasoning is right for SIDE_DECISION_REGIMES, which the
  build queue holds to the voted side. It is wrong for MEAN_REVERSION_REGIMES,
  which do not follow the vote: scored on the side AGAINST it, `range` got no
  penalty precisely because the vote had decided something `range` ignores.
  The two changes were written four months apart and compose badly; with the
  regime unable to qualify, nobody could see it.

  Driving the real `entry_signals` over 2026-09-18, 09-21 and 05-28, 6 of 11
  `range` signals faded their own symbol's day by >= 0.30%, three by >= 1% —
  QCOM LONG on a -6.85% day, META LONG on -2.61%, GOOG LONG on -1.59%. When
  the vote decides, the penalty now still applies to MEAN_REVERSION_REGIMES
  (scaled by the day's move exactly as `_bias_penalty` documents; zero in the
  neutral band and for a fade WITH the day), and remains skipped for the
  vote-bound regimes. Same sessions after: 5 signals, 0 fading their own day
  by >= 0.30% — the remaining five are neutral-day fades or trend-aligned
  ones (buying a dip on an up day). It is logged as `mr_bias_pen`, separately
  from `bias_pen`, so a skip line does not read as though trend was docked.

  Two things this does not do, stated so they are not assumed. The penalty
  caps at 1.0 and `range`'s headroom is exactly 1.0 (5.0 - 4.0), so a PERFECT
  5.0 fade still clears at the floor on a deep down day — the penalty's own
  docstring says it filters "most" counter-bias setups, not all; none of the
  three sessions produced one. And it keys on the symbol's own move, not the
  sector's: two of the five survivors were flat names on a -0.9% sector day,
  which reads as relative strength rather than a knife.

  Also on this pass: one new test took 14s a side because its tape builder
  reloaded the preset ~420 times (now cached, ~2s); the preset's headroom
  comment still gave `range` 1.5 after its floor moved to 4.0; and the
  README's Range bullet predated both the scorer's squeeze gate and the new
  floor. The 81 "no regime qualified" lines that printed `range` at or above
  4.0 were NOT a scoring mismatch: SHORTs clear `min_range_score` plus
  `short_min_score_premium` (0.5), so a short `range` needs 4.5 and the regime
  now leans long by design.

  Coverage: 1,811 passing (+5). The tests drive `entry_signals` itself, since
  the defect was in how it composed the penalty with the vote rather than in
  `_bias_penalty`, which was already unit-tested and correct. Each revert is
  caught by the test aimed at it: removing the fix fails the deep-down-day and
  skip-line tests, and over-applying it to every regime fails the one that
  pins trend's exemption.

- **vol_squeeze's compression gate was calibrated in the wrong units, and a
  bug pass found a fourth score/build mismatch.** *2026-09-22*

  **`vol_squeeze_max_range_atr` compares a TWELVE-bar box against a ONE-bar
  `atr14`** — the same units mismatch as `orb_max_range_atr_mult`, and worse.
  Measured over 167,447 RTH 1m bars across 18 archived sessions
  (2026-05-01 .. 09-22) the ratio runs:

      p5 2.17   p25 2.81   median 3.44   p75 4.26   p95 5.75   max 15.71

  The shipped `1.8` sits BELOW the 5th percentile, so 0.79% of bars could ever
  be "compressed" and the regime reached its full gate stack 3.4 times a
  session across 28 symbols. A random walk puts the expected 12-bar range near
  3.3x a 1-bar ATR, so the measured median is the NEUTRAL box width, not a
  tight one. Now `2.8`, the p25 — the same definition
  `bollinger_squeeze_width_pct` uses at its own p25, so the regime's two
  compression tests finally mean the same thing. Replaying 2026-09-21 takes
  the regime from 3 qualifying candidates to 44, none of which the builder
  would reject for a weak breakout. The breakout buffer is deliberately NOT
  touched in the same change: this regime's archive is 11 trades for +$9.58
  that becomes -$120 without one TSLA winner, so it gets one variable at a
  time.

  `vol_squeeze_max_range_pct: 0.012` admits 95.59% of bars and never binds. It
  is kept as the absolute backstop for a name whose `atr14` is unusually small
  relative to price, and is now documented as such rather than reading like a
  live gate.

  **`_score_range` still ignored one of its builder's gates.**
  `_build_range_signal` refuses outright when the tape is in a Bollinger
  squeeze (`reject_range_during_squeeze`) and the scorer never looked, so a
  squeezed bar could score the full 5.0, win the auction and die on
  `range_bollinger_squeeze` — 25 of those on 2026-09-21 even after the
  threshold recalibration, and 387 of 387 before it. Fourth instance of this
  shape in a week, after ORB's bare-break +2.5, the range regime's optional
  prev-bar confirmation and vol_squeeze's three breakout bonuses. `_score_range`
  now takes `tech_ctx` (required, not optional — an optional one is a caller
  that can silently forget it) and mirrors the gate, flag and all.

  **`min_range_score` re-derived, 3.5 -> 4.0.** A score floor only means
  something relative to the scorer it gates, and the scorer was replaced. At
  3.5 the minimum qualifying setup was zone + prev-bar + rejection wick and
  nothing else: a fade off the range edge with NEITHER piece of evidence that
  the tape is ranging rather than trending, which is the falling knife the two
  +0.5 context components exist to tell apart. 19 of the 28 range setups on
  2026-09-21 and 9 of 26 on 09-18 scored exactly that. 4.0 requires 1.5 beyond
  the zone — the wick plus any one +0.5, or all three together. 4.5 measures
  back to 0 setups on 09-21 and 2 on 09-18, i.e. the dead-regime state this
  week's work exists to fix, which is the `min_pullback_score: 4.0` and
  `min_sr_scalp_score: 4.0` mistake in the other direction.

  **Left alone, and worth stating plainly: `range` now takes most of the
  book.** Driving the real `entry_signals` over full archived sessions with
  all 28 frames, it is 62.5% of the signals on 2026-09-21 (down from 85%
  before the squeeze gate and the floor) and 97.0% on 09-18. That is NOT the
  auction normalisation — it is that `range` is the only regime exempt from
  BOTH `require_entry_confirmation_bar` and `require_index_confirmation`, and
  those two account for ~700 of the directional regimes' rejections across the
  two sessions. The exemption is deliberate and documented; it simply never
  mattered while the regime could not qualify. Narrowing it, or capping
  concurrent mean-reversion positions, is a strategy decision and not
  something a bug pass should make on its own.

  Coverage: 1,806 passing (+9). The new assertions pin the calibration the way
  the squeeze-threshold ones do — the ATR cap must sit above the 5th
  percentile of the ratio it gates and below its median, or it is either
  always-false or not selective; the pct gate must stay above the tape's own
  p95 so its backstop role is a conscious choice; and the range floor must be
  both unreachable-by-a-bare-fade and reachable by a real one.

- **`vol_squeeze` could qualify a setup its own builder was certain to
  reject.** *2026-09-22* — the preset records the 2026-05-14 change as "convert
  vol_ratio / close_pos / buffer from +0.5 bonuses to HARD gates". Only
  `_build_vol_squeeze_signal` was changed. `_score_vol_squeeze` kept all three
  as optional bonuses, so a setup with none of them scored 2.0 box compression
  + 1.0 BB compression + 0.5 both-agree + 0.5 VWAP alignment = exactly
  `min_vol_squeeze_score` (4.0) — it cleared the floor, won its place in the
  build queue and then died on `vol_squeeze_weak_breakout_*`.

  Measured: **1,838** such rejections across the 2026-09-21 and 09-22 sessions,
  every one of them `weak_breakout_buffer` — i.e. a setup that qualified as a
  squeeze BREAKOUT with no break at all. The queue falls through to the next
  regime, so this did not lose trades outright; it inflated the normalised
  score that orders the auction, and made a guaranteed-fail regime the
  `entry_family` recorded against the skip — corrupting the attribution added
  on 2026-09-19 to tell a tuning problem from a dead regime.

  Third instance of this shape this month, after ORB's bare-break +2.5 and the
  range regime's optional prev-bar confirmation, so the fix follows the same
  pattern: one derivation, `_vol_squeeze_breakout_quality`, read by the scorer
  and the builder. Clearing all three gates is now the +1.5, and the freed
  bonuses re-point at DEGREE — +0.5 for a break at twice
  `vol_squeeze_breakout_buffer_pct` and +0.5 for volume at 1.5x the required
  ratio — so the score still discriminates among qualifying setups. The ceiling
  stays 6.5, which matters more here than elsewhere: vol_squeeze has the widest
  headroom of any enabled regime, so its ceiling drives its ranking. Compression
  alone now caps at 3.5, below the floor. `vol_squeeze_hard_breakout_gates:
  false` reverts BOTH, because the point is that they agree under either
  setting.

  Coverage: 1,797 passing (+24). `tests/test_vol_squeeze_regime.py` sweeps the
  grid of break strength x breakout volume x bar close position and asserts
  nothing the scorer qualifies is rejected by the builder for a weak breakout,
  on both sides and under both settings of the flag, with the builder's failure
  tags pinned so re-deriving them from the shared helper cannot silently rename
  one.

- **Neither mean-reversion regime could take a mean-reversion trade.**
  *2026-09-22* — the strategy declares two, `range` and `sr_scalp`, against a
  preset whose own universe thesis is that these names are "VWAP-reverting with
  occasional trend days". Three independent defects, one per stage of the
  funnel, and all three had to go for a support bounce or a resistance
  rejection to reach an order.

  **`_score_range` measured close to the opposite of what its builder gates
  on.** `_build_range_signal` enters only from the outer 35% of the lookback
  range; the scorer never looked at where in the range price was, and paid its
  largest single component (+1.5 of a 5.0 ceiling) for sitting within 0.20% of
  VWAP — the middle. Replayed over 10,162 real 1m bars (28 symbols,
  2026-09-21) it correlated **-0.17** with distance from mid-range, averaged
  1.94 at an edge against 2.19 mid-range, and blocked 6,485 of the 7,477 bars
  that were actually at an edge. It also took no `side`, so LONG and SHORT
  scored identically and the auction could not tell which edge price was on —
  every other regime scorer is side-aware.

  This is the defect `_score_sr_scalp` was redesigned out of on 2026-05-29
  ("measured chop character ... UNCORRELATED with the level geometry the
  builder actually gates on"), in the other mean-reversion regime; the lesson
  was never carried across. Same fix: score the builder's geometry, keep the
  character checks as corroboration. The fade zone now comes from one helper,
  `_range_entry_zone`, that both the scorer and the builder call — the
  `_breakout_reference` precedent from two days ago. Prev-bar confirmation
  moved from an optional +0.5 to part of the hard +2.0, because the builder
  demands it: a single-bar poke scored 3.5, cleared the floor, won its place in
  the build queue and then died on `not_near_range_low_prev_bar`. The freed
  +0.5 marks a deep rather than marginal fade. After: correlation **+0.68**,
  mean 3.04 at an edge against 0.50 mid-range, and 100% of qualifying bars in
  the builder's entry zone. `range_max_vwap_dist_pct` is removed.

  **`bollinger_squeeze_width_pct: 0.06` was not a threshold on this universe.**
  `bollinger_width_pct` is `(bb_upper - bb_lower) / bb_mid`, a FRACTION of
  price. Across 162,056 RTH 1m bars over 17 archived sessions (2026-05-01 ..
  09-21) it runs median 0.0041, p95 0.0167, max 0.236 — so 0.06 flagged
  **99.87%** of all bars as "in a squeeze". The flag PARTITIONS the two
  regimes: `_score_vol_squeeze` reads it as the compression to trade and
  `_build_range_signal` reads it as the condition to refuse. At 99.87% that
  partition was degenerate — vol_squeeze owned every bar and `range` owned
  none, which is why one session logged 387 of 387 range build failures on
  `bollinger_squeeze` and the regime has a single trade in the whole archive.
  The preset now sets 0.0025, the p25 of the measured distribution, and the
  flag fires on 25.7% of bars. `vol_squeeze_max_width_pct` moves 0.05 ->
  0.0035 for the same units reason: it is the OR-branch of the same test, and
  at 0.05 it was true on ~100% of bars, making `_score_vol_squeeze`'s +1.0
  compression point and its +0.5 "both signals agree" bonus free — 1.5 of a
  4.0 floor paid for nothing. **Preset only**; the code default in `config.py`
  is untouched, so no other strategy is affected.

  **`sr_scalp`'s stop floor bound on every setup, not the noisy ones.** The
  flat `sr_scalp_min_stop_atr_mult: 2.5` installed on 2026-07-29 after META
  07-28 (three shorts into a band the tape chopped 11.9 ATR through, stops
  1.09-2.70 ATR away, 63% of the window's bars trading through them) was the
  right diagnosis with the wrong instrument: the geometric stop here is at most
  proximity 0.4 + zone half-width 0.2 + `level_buffer` ~0.3 = about 0.9 ATR
  from entry, so 2.5 always won. The level picked the direction and was then
  discarded on the risk side, which is not what an S/R scalp is. It also made
  `sr_scalp_min_distance_atr: 2.0` a fiction — a flat 2.5 ATR risk against a
  target pinned to the opposing zone needs 2.4-3.2 ATR of gap to clear
  `min_target_rr`, so a setup at the documented floor could never build. Two
  floors with one silently dominant is the shape that also hid inside ORB.

  The floor is now measured against the level's own violation history: how far
  price has PIERCED it over `sr_scalp_noise_lookback_bars` (new, 20). A level
  that has been holding keeps its own tight stop; a level being cut through
  pushes the stop out past the breaches and, because the reward cannot stretch,
  the R:R check then rejects the setup — the META case, rejected for the right
  reason. `sr_scalp_min_stop_atr_mult` drops to 0.5 as an absolute backstop,
  and the rejection reason now names which floor bound (`bound_by=pierce|atr`).

  A same-day bug check found the first cut counting pierces from the start of
  the window, which includes a fresh flip's approach from the far side.
  FLIP-CONTINUATION leans on a confirmed-flipped level by definition, so it
  died on `stop_floor_kills_rr` whenever the break was inside the lookback
  (reproduced: `bound_by=pierce`, R:R 0.97 on a clean flip). Pierces now count
  from the first close on the entry side of the level, the crossing bar itself
  excluded; a flip that is later closed back through is still priced in. Found
  with the regime switched off and fixed anyway, so re-enabling it does not
  inherit it. Pinned by `TestSrScalpPierceStartsWhenTheLevelTookItsRole`, with
  the old window and two plausible wrong fixes (counting the crossing bar,
  restarting at the last reclaim) each caught.

  `disable_sr_scalp_regime` stays `true`. The geometry defect is fixed, so
  re-enabling it is now a trading decision rather than a bug fix, and the
  current week is already measuring the armed retest. `range` is enabled and
  its fixes are live.

  Coverage: 1,773 passing (+27). `tests/test_mean_reversion_regimes.py` pins
  the properties rather than the instances — that everything the scorer
  qualifies is in the builder's entry zone across the whole band of positions,
  that both follow a retuned `range_entry_zone_frac` together, that the squeeze
  threshold sits inside the measured distribution in both directions, and that
  a setup at the advertised gap floor builds — with a control that restores the
  old flat 2.5 and confirms the same setup is rejected, so the test cannot pass
  against the unfixed code.

- **Four logic defects in the ORB regime.** *2026-09-22* — found while
  evaluating why it has never worked. The headline answer is that **it has
  never run**: every attempt in the archive died on `orb_range_too_wide`, 3,403
  of them, with not one of any other ORB failure reason ever logged. That is a
  calibration problem — `orb_max_range_atr_mult: 4.0` compares a 15-bar opening
  range against a 1-bar `atr14`, and across 462 real symbol-days the median
  ratio is 4.64, i.e. the cap sits below the median. Left alone; enabling ORB
  is a trading decision and it ships disabled.

  The four logic defects underneath it were fixed, since all would have
  surfaced the moment the cap was corrected:

  * `_score_orb` awarded its +2.5 for a BARE break while `_build_orb_signal`
    required the break to clear `orb_breakout_buffer_atr_mult`. A one-tick poke
    scored 4.0, cleared the 3.5 floor, won its slot in the build queue and died
    on `orb_no_break_above` — a guaranteed-fail path, not an optimistic one.
    The +2.5 now requires the buffered break; the +1.0 marks a decisive break
    at 2x the buffer; the ceiling stays 5.0.
  * `_time_in_range` is inclusive at both ends, so the range-formation window
    overlapped the ORB window at `orb_range_end`, and formation returns first —
    making that minute unreachable. The window ran 09:46-10:05 while
    `entry_windows` opened at 09:45. The formation check is now half-open.
  * `orb_range_minutes` and `orb_end_time` had an unvalidated implicit
    ordering. Violating it silently shrank the window rather than erroring: 30
    minutes left 5 tradeable minutes, 34 left 1, 35 left none, with no error
    and no log line. `_validate_orb_window` raises at construction, only when
    the regime is enabled.
  * The range-end derivation was written out in three places. They agreed, but
    three copies of one rule is how the bot forms the range over one span and
    opens the window against another.

  Coverage: `tests/test_orb_regime.py` (22). Each fix was reverted
  individually and confirmed to fail its own tests — the first attempt at that
  check silently overwrote its own backup after a crash and left one fix
  reverted in the tree, so the verification is now driven from an immutable
  copy.

- **The EOD report's headline described the ACCOUNT, not the session.**
  *2026-09-21* — the same bug fixed in `manifest.realized_pnl` on 2026-09-19,
  in the call site that fix missed one function over.
  `account.capture_snapshot()` returns `account.realized_pnl`, a lifetime
  accumulator set to 0.0 once in `PaperAccount.__init__` and only ever
  incremented, and every headline number in `SESSION_REPORT` and the human log
  line read from it — pnl, wins, losses, win_rate, profit_factor,
  average_trade — while `trades` counted the list beside them. Two scopes in
  one line, the wrong one as the headline, which is the exact wording of the
  earlier fix.

  `closed` was also filtered only to final exits, never to the session date, so
  on a bot running across midnight without restarting the persistent
  `trades.csv` **re-appended the prior day's rows under today's date**. Both
  now use the same date filter `export_session_archive` already applied, so the
  report, the CSV it writes and the archive all describe one day. The headline
  sums the ROUNDED per-row values, so `realized_pnl` equals the sum of
  `trades.csv` exactly rather than to within a cent.

  A trade whose `exit_time` cannot be read is dropped rather than kept. The
  first version kept it, on the "unevaluable is not absent" principle — and a
  test written to assert that failed, because `_trade_csv_row` calls
  `exit_time.isoformat()` and one bad record raises inside the broad
  try/except around the whole block, costing the entire report. Losing a row
  beats losing the session.

- **An unreadable timestamp could take down the report and the dashboard.**
  *2026-09-21* — found while testing the above, and upstream of it.
  `PaperAccount._trade_to_dict` called `trade.exit_time.isoformat()`
  unguarded, and it runs inside `capture_snapshot`, which is the FIRST thing
  `write_session_report` does — so the session-date filter could never protect
  against it. One record with a bad timestamp destroyed the whole EOD report,
  and the dashboard snapshot shares the same path. Both timestamps now
  serialize to null instead of raising.

- **An expired arm could never produce its entry.** *2026-09-21* — found on
  day one of the armed retest, from a live miss. The market fallback exists so
  a runaway move that never offers the retest still gets traded; it was
  evaluated inside the build queue, AFTER the side / index-confirmation /
  confirmation-bar gates. The moment any of those lapsed while the arm waited,
  `_armed_retest_verdict` stopped being called and the 12-minute expiry was
  never reached.

  INTC: armed 09:58 at 118.39 on a setup that had passed every gate — only the
  arm held it back — the sector index lapsed at 10:03, and the stock ran 117.26
  → 124.64 with no entry. `touched=0` throughout, so the retest genuinely never
  came and the fallback was the whole point. It never fired once.

  The asymmetry that made it possible: index confirmation is an ENTRY gate, and
  once in a position it no longer applies. Arming holds the trade OUT across
  exactly the window where a lapse can lock it out, so a setup that had already
  cleared the gates is re-validated against them and can fail. The fallback has
  to be judged on the arm-time decision or it is not a fallback.

  Expiry now lives in `_expired_armed_retests`, which runs before the queue and
  is exempt from those three gates; the verdict keeps `none` / `wait` / `enter`
  and no longer reports expiry at all. The arm carries the `regime_score` it
  was validated with, so the fallback builds from the score the setup HAD
  rather than a fresh one that asks a different question. Still enforced: the
  regime must be offered in the current window, and the builder's own checks
  run unchanged (`breakout_confirmed` False → a faded setup still fails
  `no_fresh_breakout`; `_finalize_signal` still applies the stretched / SR /
  structure rejections).

  `skip_details` now reads the combined queue rather than `build_queue`, so a
  cycle whose only attempt was a fallback reports the regime that was tried
  instead of `none_qualified`.

  Note on the test: the first version of the regression passed against the
  UNFIXED code, because the synthetic decision for an expired arm set
  `index_ok: True` — a fake value that smuggled the exemption through the gate
  and made the test vacuous. It now carries `index_ok: False` and the exemption
  is `pre_validated`, explicitly, so reverting the fix fails the test.

  Coverage: 1,712 passing (+7).

- **An armed retest could go stale and then justify a market entry.**
  *2026-09-20* — two holes in the arm lifecycle, both found by walking the
  state transitions rather than the happy path. An arm past its window is a
  licence to enter at market on the next qualifying cycle, so how long one can
  survive is a trading question, not bookkeeping.

  * **A held position did not clear the symbol's arms.** An arm can never
    produce the entry it was created for once a position exists, and
    `_prune_armed_retests` kept it well past its window — so when the position
    closed, twenty minutes later and at a different level, the next qualifying
    cycle found an EXPIRED arm and took the market fallback immediately. The
    re-entry, which is the most chase-prone entry there is, was the one entry
    guaranteed to skip the wait. `entry_signals` now drops a symbol's arms
    when it is already held.
  * **The reaping window was 3x (42 minutes).** The regime can go a long time
    without qualifying — index confirmation lapses, the score dips — so a
    fallback entry could be justified by a breakout most of an hour earlier at
    a level the tape had moved away from. Now 2x the configured wait: past
    that the arm is dropped and the next qualifying cycle arms again, waiting
    rather than entering on stale evidence.

  Both are in the safe direction — the bot waits where it previously would
  have entered — and `per_entry_path` will show the effect as a shift from
  `market_fallback` toward `retest`. Coverage: 9 tests over the lifecycle,
  including that `AA` must not drop `AAPL`'s arms.

- **`entry_timing`'s baseline sampled the wrong tape.** *2026-09-20* — found
  in a second bug pass, after the metric had already been published. The
  docstring claimed a same-session comparison, but `bars_for` hands back the
  MULTI-DAY history frame and the sampler walked all of it — folding quiet
  overnight and prior-session bars into the baseline, depressing it, and
  inflating every edge measured above it.

  The whole metric is the gap between observed and baseline, so a baseline
  drawn from a different tape measures nothing. Now scoped to the trade's own
  session date. Every published number moved:

  | metric | before | after |
  |---|---|---|
  | overall baseline | 0.352R | 0.358R |
  | `trend` edge | +0.715R | **+0.641R** |
  | `sr_scalp` edge | +0.432R | +0.410R |
  | `pullback` edge | +0.249R | +0.213R |
  | `vol_squeeze` edge | +0.008R | **-0.024R** |
  | baseline samples | 8,140 | 3,477 |

  The conclusions hold and sharpen — `trend` still chases by a wide margin and
  `vol_squeeze` now reads as slightly BELOW an arbitrary moment, i.e. no edge
  at all rather than a sliver of one. Every citation of the old figures in the
  READMEs, the preset comment, the strategy docstrings and this file was
  corrected in the same change; leaving docs quoting a measurement the code no
  longer produces is how a number outlives the analysis that made it.

- **A confirmed armed retest could never build a signal.** *2026-09-20* —
  found in the final bug pass on the same day the feature shipped, before any
  dry-run. `_build_trend_signal` / `_build_momentum_signal` reject unless
  `close > max(high of the previous N bars)`. On the retest cycle that window
  STILL CONTAINS THE BREAKOUT BAR, whose high is above the reclaim close —
  which is what a retest *is*. So every confirmed retest died on
  `no_fresh_breakout`, and the feature could only ever produce expiry/market
  fills: precisely the chasing behaviour it was built to remove.

  Both builders now take `breakout_confirmed`, set only when
  `_armed_retest_verdict` returns `enter`. The arm already recorded the
  breakout and the verdict only confirms with close back above that level, so
  the gate is satisfied by construction rather than skipped — an EXPIRED arm
  passes `False` and must still clear it on its own, or a faded setup would
  enter where the bot would never have traded before.

  The end-to-end tests missed it because they stubbed the builder to isolate
  the wiring. `tests/test_armed_retest.py::TestTheRetestCanActuallyBuild` now
  drives the real builder on a real retest tape, and pins the rejection with
  the flag off so the fix cannot silently regress.

- **The armed retest would have switched itself on for `small_cap_squeeze`.**
  *2026-09-20* — `SmallCapSqueezeStrategy` subclasses `TopTierAdaptiveStrategy`
  and runs `trend` and `momentum` as its only two entry regimes, so a code
  default of `True` would have converted both of its entry paths to
  arm-and-wait with nobody choosing that — the same trap the vwap_reclaim knob
  shipped opt-IN to avoid on 2026-05-30, and its 2026-06-02 dry-run is the
  baseline for the next one. Now declared `false` in both its preset and its
  manifest, with a test that pins the premise (that it still runs the two
  arming regimes) alongside the opt-out, so the guard fails loudly rather than
  passing vacuously if that changes.

- **Eight params resolved off a code default.** *2026-09-20* — `zone_pct`,
  `zone_atr_mult`, `range_max_intraday_range_pct`,
  `range_require_prev_bar_confirmation`, `trailing_bias_enabled`,
  `trailing_bias_lookback`, `trailing_bias_majority_threshold` and
  `extended_hours_tradable_all` were read by the strategy and declared in
  neither the shipped preset nor the manifest. Live gates whose state could
  only be discovered by grepping the strategy, and editing any of those
  defaults would have silently retuned every preset at once — how
  `disable_orb_regime` came to say ORB-on as the config-less baseline while
  every preset turned it off. All declared at the value they were already
  resolving to, so behaviour is unchanged and the defaults are now inert.

  `tests/test_param_declaration_drift.py` pins the property rather than the
  list: every `self.params.get(...)` name must appear in the preset or the
  manifest, with a guard test that fails if the scan itself stops matching.


- **The session manifest's realized PnL described the account, not the
  session.** *2026-09-19* — `manifest.realized_pnl` read
  `account.realized_pnl`, a LIFETIME accumulator: set to 0.0 once in
  `PaperAccount.__init__` and only ever incremented, with no per-day reset.
  Beside it, `trades_today` and `trades.csv` are filtered to the session date.
  Two scopes in one manifest, and the wrong one is the headline number.

  Across the archived sessions that traded, **5 of 10 disagreed with their own
  `trades.csv`**, in both directions. 2026-07-31 shows the mechanism
  arithmetically: manifest −80.77 against a CSV summing to −60.22, a gap of
  exactly −20.55 — the previous session's PnL, carried forward by a bot that
  ran across both days without restarting. Four other sessions reported 0.00
  while holding real trades, understating a +13.59 day and a −122.80 day
  alike.

  The manifest now sums the same date-filtered list that produces `trades.csv`
  and `trades_today`, so `manifest.realized_pnl == sum(trades.csv)` holds by
  construction. Summing the ROUNDED per-row values (rather than rounding the
  sum) is deliberate — that is what `_trade_csv_row` writes, so the equality is
  exact rather than within a cent.

  A failed CSV export now reports `null` and a new `trades_export_error` field
  rather than leaving the initialized 0.0 in place. `_trade_csv_row` reads
  every `TradeRecord` field by name, so one record missing a field added later
  raises and is swallowed as a warning — and a wrong-but-plausible zero is
  worse than an absent value, because nobody investigates a zero. This is the
  likeliest explanation for the four 0.00 sessions.

- **A screener row with no usable ticker became a candidate.** *2026-09-19* —
  `_candidate_rows` built its symbol with `str(row.get("name"))`, which turns a
  missing value into the literal symbol `"None"` and a NaN into `"nan"`. The
  engine drops an EMPTY symbol from the watchlist but not a non-empty junk
  one, so the bot would fetch history and quotes for a ticker that does not
  exist and show it on the dashboard. Every screener's rows arrive from an
  external API, so the ticker is not ours to trust. New `_candidate_symbol`
  rejects genuinely absent values and normalizes the rest; dropped rows are
  counted and logged, because silently discarding screener rows would hide a
  TradingView schema change. Deliberately no blacklist of junk-looking tokens
  — that risks excluding a real ticker, so a ticker with no exchange prefix is
  still kept.

- **A raising strategy callback took down the whole screener run.**
  *2026-09-19* — `_candidate_rows` wrapped only the `float()` conversion in its
  try, leaving the `activity_score_fn(row)` CALL unguarded, and
  `directional_bias_fn` had no guard at all. Both belong to the strategy
  plugin, so one bad row's exception aborted the entire candidate list rather
  than that candidate. Both are now wrapped, warning once per cause per
  process — they fire per candidate per cycle, so unthrottled they would flood
  the log.

- **An unscored candidate list came back in reverse.** *2026-09-19* — with no
  `activity_score_fn` the score defaulted to the row's ordinal, and
  `_candidate_rows` sorts DESCENDING, so the last row of a screener's
  "best first" `order_by` came out on top. Unscored candidates now all get
  0.0 and the existing `-candidate_query_order` tiebreak restores the
  screener's own order. Latent — all 12 shipped call sites pass
  `activity_score_fn` — but it is reachable by any plugin that omits the
  callback, and it was also the fallback path the fix above just made
  reachable.

- **`small_cap_squeeze` emitted stale and duplicated candidate ranks.**
  *2026-09-19* — `_candidate_rows` numbers candidates 1..N within a single
  screen. `_merge_premarket_lock` then unions the premarket-locked set with
  the live RTH screen, re-sorts by `activity_score` and caps to
  `max_candidates` — but never re-ranked, so the output carried ranks like
  `[1, 2, 2, 3, 3]`: three candidates claiming rank 2 or 3. `rank` is the
  final tiebreak in `entry_gatekeeper._signal_priority_key` and is what the
  dashboard candidate card and the audit log's `candidate_rank` display, so
  the visible damage is diagnostic rather than directional — it only reaches
  execution order on an exact score tie. The merge now re-ranks and uses the
  same `(score, -query_order)` tiebreak `_candidate_rows` applies, instead of
  a plain stable sort that put locked-but-faded names ahead of equally scored
  fresh ones. The only screener with the problem: the other 17 either sort and
  cap the DataFrame BEFORE `_candidate_rows`, or build candidates manually and
  reassign `rank` after sorting.

- **`top_tier_adaptive`'s screener read the ACTIVE strategy's params.**
  *2026-09-19* — `config.active_strategy.params` rather than
  `config.strategies[self.strategy_name].params`, the sole outlier among 18
  screeners. Identical today: `get_candidates` has exactly one caller
  (`engine._run_cycle`, passing `config.strategy`), so the screener is only
  ever built for the running strategy. But the failure mode if that stops
  holding is silent — another strategy's params carry no `tradable`, so `run()`
  returns an empty universe with no error.

- **Indicator lengths now mean the same thing on a span-scaled frame.**
  *2026-09-19* — `add_indicators(span_scale=N)` stretches every bar-count
  lookback but keeps the nominal column NAMES, so on `top_tier_adaptive`'s 1m
  LTF (`ltf_indicator_span_scale: 5`) `adx14` is a 70-bar ADX and `bb_*` are
  100-bar bands. `build_technical_levels_context` reads those columns when the
  requested length matches the nominal one and recomputes otherwise — and the
  recompute ran at the NATIVE length. `adx_length: 14` therefore meant ADX-70
  while `adx_length: 15` meant a true ADX-15: a one-digit config change
  swapping the lookback by 5x with nothing to warn on. Four parameters had the
  shape — `adx_length`, `bollinger_length`, `obv_ema_length` and
  `divergence_rsi_length`. On a 400-bar span_scale=5 tape, 14 vs 15 drifted
  59.7% on ADX and 86.4% on Bollinger `width_pct` (band width off by ~7x);
  after the fix, 8.9% and 2.3% — neighbouring lookbacks, which is what one more
  bar should mean. `add_indicators` now stamps the applied scale on
  `frame.attrs` and each `*_length` is treated as nominal, with the fallback
  computed at `nominal x span_scale`. The stamp is written unconditionally,
  including at 1.0: pandas propagates `attrs` through `__finalize__`, so a
  frame resampled off a stretched one inherits the stale scale while losing the
  stretched columns, and only an unconditional write on rebuild clears it. New
  `scaled_span` is the single definition of that arithmetic — `add_indicators`
  builds with it and every consumer recomputes with it, so producer and
  consumer cannot drift. LATENT when found, on two independent axes: all 19
  shipped configs and the `config.py` defaults sit on 14 / 20 / 2.0 so the fast
  path always won, AND no shipped call path hands a stretched frame to
  `build_technical_levels_context` in the first place — `top_tier_adaptive`
  passes the base 1m frame (as its README records), both `peer_confirmed_*`
  strategies build their LTF at scale 1.0, and the dashboard's `get_merged`
  call does not pass a scale. Nothing changes at `span_scale` 1.0; this closes
  the trap before either axis moves, and the `small_cap_squeeze` README
  actively invites one of them ("Raise `ltf_indicator_span_scale` toward 3-5").
  Deliberately NOT scaled: the
  `*_lookback_bars` / `pivot_span` window sizes, which have a single
  computation path and therefore no second path to disagree with, and
  `bollinger_std_mult`, which multiplies a standard deviation rather than a bar
  count. `add_indicators` also now rejects a zero, negative or non-finite
  `span_scale` by name instead of letting it surface as "TA_BBANDS function
  failed with error code 2: Bad Parameter" from inside TA-Lib.

- **Chart pattern detection could report a pattern that was not there.**
  *2026-09-19* — `chart_patterns._CHART_HELPER_CACHE` keys on `id(frame)`,
  which only identifies an object while it is alive: CPython hands a freed
  address to the next allocation of the same size. Three call sites built a
  slice that died inside the helper it was passed to (`_pivot_order`'s
  `frame.tail(10)`, and `f.tail(14)` in both symmetrical-triangle detectors),
  so a later `_tail(frame, 40)` could land on the dead slice's address and read
  ITS `_mean_range` — which sets `_level_tolerance` and the `_find_pivots`
  prominence floor. Instrumented over 400 analysis calls, 21 served at least
  one stale value; against a cache-free reference over 1500 frames, one frame
  gained a `bullish_ascending_triangle` the bars did not support, taking the
  context from bias 0.0 "neutral" to 1.1 "bullish". Nondeterministic — the same
  bars give a different answer depending on heap layout, which is why it never
  showed up as a reproducible complaint. Fixed structurally rather than site by
  site: the cache now pins every frame it keys on, so no address can be
  recycled while an entry names it. Benchmarked at no measurable cost (-1.9%,
  within noise, over 200 calls).

- **Bollinger context fields returned NaN where they promised `float | None`.**
  *2026-09-19* — the guard was `not series.dropna().empty` — "does this series
  hold a value ANYWHERE" — followed by an unconditional `.iloc[-1]`. Ten
  instances in `technical_levels.py`. A symbol that stops ticking for
  `bollinger_length` bars has zero rolling std, so `bb_width` is 0 and
  `percent_b` / `zscore` divide by it, and `ctx.bollinger_percent_b` came back
  as `float('nan')` rather than `None`. NaN fails every comparison, so
  `strategy_base`'s `bb_pct is None or float(bb_pct) <= 0.82` became *neither*
  and silently dropped the mid-band entry-score bonus; it also made the audit
  log invalid JSON. Replaced with a `_last_value` helper that checks the value
  actually read.

- **The exported candle detectors were starved of context.** *2026-09-19* —
  `detect_bullish_patterns` / `detect_bearish_patterns` sliced to 3 bars while
  `detect_candle_context` used `CANDLE_CONTEXT_BARS` (30). TA-Lib builds
  average-body and trend state from preceding bars and emits 0 inside its
  warmup, so 3 bars starves nearly every pattern: over 600 tapes the 3-bar
  slice fired on 88 where the 30-bar slice fired on 441. Neither has an
  in-repo caller, but both are re-exported from `_strategies/shared.py`, so a
  plugin author reaching for them inherited the shortfall with no error to go
  on. Both now use `CANDLE_CONTEXT_BARS` and agree with `detect_candle_context`
  exactly.

- **Tweezer patterns expired three times faster than every other 2-bar
  pattern.** *2026-09-19* — TA-Lib patterns stay reportable for three bars
  after completing (`_talib_pattern_value_from_key` scans outputs -1, -2, -3),
  but the two custom tweezers only ever compared the final pair, so they
  vanished one bar after completing. Same tier, same registry, a third of the
  lifespan. It reached the tier cascade, which returns only the longest tier
  that fired: a tweezer ageing out early let a 1-bar match win and downgraded
  the confirm tier from `solid_2c` (weight 0.70) to `weak_1c` (0.35) on bars
  where a TA-Lib 2-bar pattern would still have held. Both now share a
  `CANDLE_PERSISTENCE_BARS` constant that the TA-Lib scan loop derives its own
  window from, so the two cannot drift. The per-bar dashboard map stays
  completion-only — persistence belongs to the snapshot path, and smearing a
  match forward would mark bars the pattern did not complete on.

- **`adaptive_ladder` left the target behind price on fast moves.** *2026-09-19*
  — `_adaptive_ladder_management` stepped `ladder_active_index` to
  `active_index + 1` unconditionally, while the guard beside it correctly
  refused to set a target UNDER price. When one cycle cleared several rungs at
  once the index walked forward and the target stayed on the first rung, so
  `update_position` saw `last_price >= target_price` and exited the remainder.
  Walked a LONG from 100.5 to 104.9 against rungs at 101/102/103/104: the index
  stepped 1, 2, 3 while the target stayed 101.00 the whole way. The ladder
  therefore cut the position at rung 1 on exactly the fast moves it exists to
  ride, which matches the 2026-06-01 dry run where runners came in around 1R
  against 3-4R of MFE. New `_next_unpassed_rung` advances to the first rung
  price has NOT passed, and promotes straight to a runner when price has
  outrun the whole ladder. The orderly one-rung-at-a-time path is byte
  identical. Found by walking the state machine, not by inspection.

- **`adaptive_ladder` could promote a stop the quote had already passed.**
  *2026-09-19* — the promotion guard validated the candidate stop against
  `close`, taken from the management frame, while the exit check in
  `RiskManager.update_position` runs against the quote snapshot. Those are two
  different sources and diverge on a fast move, so a rung price had already
  fallen back through still promoted, and update_position stopped the trade out
  on the same cycle at a price well past the new stop: rung 1 at 101.00 with
  the bar closing 101.50 and a 99.00 quote promoted the stop to 100.69 and
  exited immediately, where the original 98.00 stop would have held. The guard
  now takes the tighter of bar close and live price — `min` for LONG, `max` for
  SHORT — so such a rung simply does not promote and the position keeps its
  existing stop. Promotions where the quote is still beyond the candidate stop
  are unchanged.

- **`force_flatten` overwrote the real exit reason.** *2026-09-19* — the
  force-flatten branch in `manage_positions` ran unconditionally and reassigned
  `reason`, so every stop, target or peak-giveback that fired inside the
  force-flatten window was recorded as `force_flatten`. The position closed
  correctly either way, but the `per_exit_reason` table that tuning is read
  from had its force_flatten bucket inflated and its `stop` / `target` buckets
  hollowed out at exactly the end-of-session hour when those fire most. Force
  flatten still guarantees the exit; it now only supplies the reason when no
  other exit already did.

- **0DTE option entries now reconcile risk against the actual fill.**
  *2026-09-19* — `qty` is sized from a `max_loss_per_contract` computed before
  the order goes out, off the previewed limit, and nothing recomputed it once
  the fill was known. The equity path has done this since 2026-09-18 via
  `realized_entry_risk`; options had no counterpart. It bites hardest in dry
  runs, where `submit_option_vertical` passes `allow_natural_fill=True` to model
  a chase, so fills land worse than the sizing assumed: sweeping the shipped
  gates over 60,000 quote pairs found 20 contracts booked at $25 of max loss
  each that actually risked $38.79 each, $776 against a $500 budget. Live limit
  orders cannot fill worse than their limit, so this was a measurement problem —
  the dry-run risk figures the strategy is tuned from were quietly optimistic.
  New `realized_max_loss_per_contract` + `RiskManager.realized_option_risk`;
  results land on position metadata with a WARNING past
  `risk.risk_overage_warn_frac`. Detection only.
- **`max_net_price_frac_of_width` (new, default 0.90) gates the net price
  against the strike width.** *2026-09-19* — `max_net_spread_price` is one
  dollar cap while widths differ per symbol, and at its shipped 2.40 it sat
  above every configured width (SPY/QQQ $200, IWM $100), so it could not reject
  a quote implying a credit at or above the spread itself. That books
  `max_loss = 0`; `size_option_position` refuses it, so the outcome was a silent
  no-trade rather than a blow-up, but a degenerate chain looked identical to "no
  setup today". The new gate removed all 53 credit-above-width cases on the same
  sweep and cut worst measured exposure from +55% to +36% of budget.

- **Strategy manifests validate their window TIMES, not just their shape.**
  *2026-09-19* — `_coerce_windows` checked that each window was a list of two
  non-empty values but never that those values were parseable times, so `"9am"`
  (or a bare integer, which `str()` coerces to `"930"`) passed manifest load and
  `parse_hhmm` raised inside a trading cycle instead. Each end now parses at
  load and the rejection names the field, index, end and value. All 17 shipped
  manifests were verified clean first — 57 windows, 114 time values — so this
  only affects newly authored plugins. Overnight windows (`start > end`, which
  wrap past midnight by design) remain valid.

### Changed

- **`_decide_side`'s VWAP arm reads the same reference as `_frame_agrees`.**
  *2026-09-19* — the confirmation gate moved off session VWAP while the side
  vote did not, so the two layers disagreed about what "reclaimed VWAP" meant
  for the same symbol on the same bar. Auditing each arm against the tape's
  real direction on a V-reversal: the VWAP arm was wrong on 54 of 120 bars and
  the EMA arm on 59, while the recent-return arm — the only genuinely
  current-action signal — was wrong on 14. The two lagging arms are also the
  two that vote most reliably, because `recent` and `bars3` carry dead-bands
  and abstain often. Under `leg_anchored_confirmation` the arm now reads
  `_leg_anchor_vwap`: the vote goes from 66 ok / 18 undecided to 98 ok / 1
  undecided on a V-reversal (and 66 / 15 to 98 / 3 inverted), with trends,
  grinds and chop unchanged. The breakdown token becomes `legvwap` so a
  `side_undecided(...)` line names the reference. The EMA arm is deliberately
  untouched — its span is shared with the trend filters.
- `_frame_agrees` is no longer a `@staticmethod` (it reads `self.params`). Both
  call sites already invoked it through `self`, so no call site changed.
- `_leg_anchor_vwap` scans from the session open the rest of the strategy uses
  (`EQUITY_RTH_OPEN`, or `EQUITY_STREAM_START` in extended-indicator mode),
  matching `_day_strength_session_open`. Scanning from midnight let a thin
  pre-market print become the leg anchor while the rest of the strategy was
  still measuring from 09:30: on a frame carrying an 08:00 dump that moved the
  anchor off the session low entirely and flipped the verdict.
- `_leg_anchor_vwap` takes those bars by position rather than through
  `_same_day_mask`, which maps a Python lambda over every bar of the merged
  frame. At 12 peers x 2 sides that was ~15ms of per-candidate overhead for a
  slice `searchsorted` does in microseconds; the leg anchor now costs ~4ms.
- `EQUITY_RTH_OPEN` is re-exported from `_strategies.shared` alongside
  `EQUITY_STREAM_START`.

- **`top_tier_adaptive` retargeted at a mega-cap universe.** *2026-09-18* — the
  preset traded 23 mega caps with parameters written as if the universe were
  homogeneous. It is not: sector betas run roughly 0.5 (COST/V/TMUS) to 1.8
  (NVDA/AMD/TSLA) and ADR spans about 0.9% to 3%+, so one number was a
  different gate on every name. Seven changes, plus the two prerequisites they
  depended on.
  - **Per-symbol volatility scaling** (`daily_stats.py`, new). Every
    percent-of-price threshold is multiplied by the symbol's 20-day ADR (true
    range, so gaps count) over `reference_adr_pct`, clamped to
    `[volatility_scale_min, volatility_scale_max]`. Scaled:
    `default_stop_pct`, `momentum_min_day_strength`,
    `sr_scalp_min_distance_pct`, `relative_strength_block_threshold_pct`,
    `range_max_vwap_dist_pct`, `range_max_intraday_range_pct`,
    `vol_squeeze_max_range_pct`, `broken_level_min_clearance_pct`. ATR-multiple
    params are deliberately NOT scaled (already volatility-relative; scaling
    would square the adjustment). `momentum_min_day_strength: 2.0` was about a
    0.6-sigma move on TSLA and a 2-sigma move on COST, which made the momentum
    regime structurally a five-name strategy. Backed by
    `MarketDataStore.get_daily_history` — one Schwab daily `price_history` call
    per symbol per ET day; `vol_scale` is 1.0 (thresholds exactly as written)
    when stats are unavailable.
  - **Relative strength is now a beta residual**, `day_strength - beta *
    sector_ds` rather than a raw difference. The old form assumed beta 1.0 for
    every name and so measured beta, not alpha: on a −1.0% XLK day NVDA at
    −1.6% is performing exactly to a 1.6 beta — zero alpha — yet scored −0.6%
    and had LONG blocked. The gate was blocking high-beta names in the
    direction the tape was already moving while low-beta names essentially
    never tripped it. Beta comes from a 60-day daily regression against the
    symbol's sector ETF; when it is unavailable the gate is skipped rather than
    falling back to 1.0.
  - **Sector-breadth index confirmation.** `_index_confirms` now counts how
    many of a symbol's `sector_groups` peers lean the trade's way
    (`index_breadth_min_peers` / `index_breadth_min_agree_frac`), falling back
    to the sector ETF only for single-member sectors. The ETF test is close to
    circular on this universe — AAPL+MSFT+NVDA+AVGO are ~45% of XLK,
    GOOG+META ~45% of XLC — so it substantially asked AAPL about AAPL and
    failed in the one case that matters: the mega cap moving against its
    sector.
  - **Correlation-group concentration guard.** New `correlation_groups` /
    `max_same_correlation_group_same_direction`, replacing `sector_groups` /
    `max_same_sector_same_direction` for risk purposes (`sector_groups` stays,
    at GICS granularity, for ETF routing and peer breadth). Two LONG per sector
    across six sectors permitted up to 10 same-direction positions in names
    that run ~0.85 correlated on any macro day — one leveraged index bet
    wearing four tickers, with `max_daily_loss` reached in one move instead of
    four independent ones. The preset collapses tech + communication +
    consumer discretionary into one `mega_beta` bucket.
  - **Scheduled-event blackouts for equity strategies**
    (`event_blackouts.py`, new; top-level `events:` config section). Macro
    windows (CPI/FOMC, now optionally scoped with `symbols: [...]`) and a
    per-symbol **earnings** calendar blocking a weekend-aware
    `earnings_block_sessions_before`/`_after` window. Across 23 mega caps that
    is roughly 92 scheduled events a year.
  - **Cross-regime score normalisation.** `_normalized_regime_score` maps a
    score to its fraction of `(ceiling - threshold)` via the new
    `REGIME_SCORE_CEILINGS` table, and drives both the per-candidate build
    order and — through a new `signal_priority_key` override — the
    cross-signal slot auction.
  - **Regime trim**: `vol_squeeze`, `sr_scalp` and `orb` disabled in the
    preset, leaving trend / pullback / range / momentum.

### Changed

- **Dependency bump: schwabdev 4.0.0, TA-Lib 0.8.0, pandas 3.0.6,
  tradingview-screener 3.2.2.** *2026-09-18* — plus the security-relevant
  transitives (cryptography 46.0.7 -> 50.0.1, aiohttp 3.13.5 -> 3.14.3,
  websockets 16.0 -> 17.1, urllib3 2.6.3 -> 2.8.0, requests 2.33.1 -> 2.34.2,
  certifi 2026.2.25 -> 2026.7.22). See the requirements.txt header for the
  verification detail. Highlights: schwabdev 4.0.0 changes no API the bot
  touches, but adds request parameter validation ON by default — both
  price_history call shapes and the account-hash length rule were checked
  against it. TA-Lib 0.8.0's BBANDS/APO/PPO default changes do not reach this
  codebase (all BBANDS call sites pass their parameters explicitly); indicator
  regression showed worst absolute drift 7.5e-09 confined to three Bollinger
  columns, and two technical_levels snapshots were regenerated for the same
  round-off. **websockets 16 -> 17 is a major bump under schwabdev's streaming
  layer that the test suite cannot exercise — verify the stream on beta.**
- **Transitive dependencies are now pinned in `constraints.txt`.**
  *2026-09-18* — requirements.txt pinned only the 6 direct packages while
  schwabdev declares its four dependencies unbounded, so a rebuilt box
  resolved whatever was newest that day. Install with
  `pip install -r requirements.txt -c constraints.txt`.
- **Test and lint tooling is declared in `[project.optional-dependencies].dev`.**
  *2026-09-18* — pytest, hypothesis, flake8, vermin, vulture, pandas-stubs and
  build were installed in the working venv but declared nowhere, so
  `pip install -e .` produced a package that could not be tested. Install with
  `pip install -e ".[dev]"`.

- **`events.blackout_file` / `events.blackouts` replace
  `options.event_blackout_file` / `options.event_blackouts`.** *2026-09-18* —
  the blackout calendar lived inside `ZeroDteOptionsConfig` and was reachable
  only from the 0DTE options strategy. **Breaking for existing configs**: move
  those two keys from the `options:` block to the new top-level `events:`
  block. All shipped presets are updated. `EventBlackoutCalendar` now owns
  loading, mtime-based reload and matching; the 0DTE strategy's private copy
  is gone.
- **`_decide_side` is scoped to direction-following regimes.** *2026-09-18* —
  it collapsed `preferred_sides` for the whole candidate before any regime was
  scored, which silently removed both mean-reversion regimes: `range` enters
  within the bottom 35% of the range and `sr_scalp` at a support price has just
  fallen into, exactly where the vote's trend-following signals say the
  opposite. Simulated over a clean oscillating range with the shipped params,
  the vote agreed with the range regime's own entry zone on 3 of 54 in-zone
  bars (5.5%). The index and confirmation-bar gates already exempted these
  regimes; the vote running candidate-wide made those exemptions unreachable.
  Now gated per (side, regime) against `SIDE_DECISION_REGIMES`. New skip
  reason: `<side>_build_failed_<regime>_side_decision_opposed`.

### Changed

- **`top_tier_adaptive` retargeted at mega-cap Tech + AI, tuned for both
  directions.** *2026-09-18*
  - **Universe**: 25 names in three co-movement blocks — `ai_hardware`
    (NVDA/AVGO/AMD/TSM/MU/QCOM/ARM/MRVL/INTC/ANET/VRT/DELL), `platforms`
    (AAPL/MSFT/GOOG/AMZN/META/NFLX/ORCL/TSLA) and `software`
    (CRM/ADBE/NOW/PLTR/PANW). Dropped JPM/GS/V, LLY, COST, HD/LOW/UBER,
    TMUS/RBLX — none are Tech/AI and each dragged in an ETF that had to be
    streamed for one name's confirmation. `index_symbols` shrinks from six
    ETFs to three (SMH / IGV / XLK).
  - **Groups are by co-movement, not GICS.** META/GOOG/NFLX are Communication
    Services and AMZN/TSLA Consumer Discretionary, but across this universe
    they trade as part of the mega-cap compute complex, and peer breadth over
    that complex is a better confirmation than XLC or XLY. Every group has
    >= 5 members, so breadth (not the circular ETF check) is the live path for
    every symbol.
  - **`correlation_groups`** collapse to `ai_complex` (20) + `software` (5) at
    a cap of 2 — semis and platforms move together on any AI-narrative day.
  - **`reference_adr_pct` 0.018 -> 0.022.** The reference must sit near the
    median ADR of the traded universe or every percent threshold is
    systematically mis-scaled; the old value centred a mixed book that
    included ~1% names. `volatility_scale_min` 0.6 -> 0.55 so AAPL/MSFT are
    not clamped off the bottom, `_max` 2.2 -> 2.0.
  - **`momentum_min_day_strength` 2.0 -> 1.8**, now meaning "what a
    median-volatility name in this universe must move" — roughly 1.1% on AAPL
    and 3.4% on PLTR after scaling. The flat 2.0 made momentum a
    high-beta-only regime.

### Added

- **Long/short asymmetry knobs for `top_tier_adaptive`.** *2026-09-18* — every
  threshold was previously shared between the sides, which assumes they are
  mirror images. On equities they are not: squeezes are faster than flushes,
  the market drifts up, and short profits are less durable. Three multipliers
  encode exactly those asymmetries instead of duplicating the parameter block
  per side, and all are neutral by default so existing presets are unchanged:
  - `short_min_score_premium` (0.5) raises the regime floor for SHORTs — and
    raises the normalisation denominator too, so a short that barely clears
    its higher bar still ranks as marginal in the cross-regime auction rather
    than being flattered by the long-side floor.
  - `short_stop_buffer_mult` (1.25) widens the ATR cushion on shorts. It never
    moves the structural level the strategy chose. Dollar risk per trade is
    unchanged — the wider stop simply sizes to fewer shares.
  - `short_target_rr_mult` (0.85) banks short profits sooner: a 2.0R long
    target becomes 1.7R short.

### Added

- **Test coverage for every previously-untested module.** *2026-09-18* — the
  full-bot review found seven modules with no direct tests, several of them in
  the money path. Now covered: `startup_reconciler` (541 LOC — what the bot
  owns after a restart: ignore lists, restore eligibility, metadata matching,
  level reconstruction, the entry block and its broker recheck, and the
  restore loop), `cycle_gate` (which subsystems may run this cycle),
  `warmup_tracker` (history-fetch scheduling and readiness), `position_store`
  (sqlite round-trip, replace semantics, pruning), `_sr_ladder` (rung spacing
  and next-level selection for adaptive_ladder), `dashboard`
  (NaN/numpy-safe serialization, disk-state signature, theme-name handling,
  concurrent state access) and `audit_logger`. Every module in the package now
  has direct coverage.

### Fixed

- **ORB could emit a target on the wrong side of its own entry.**
  *2026-09-18* — found by property-testing every builder over randomised
  opening ranges. `_build_orb_signal` anchors its measured move to the RANGE
  EDGE (`edge + range_height * orb_target_range_mult`), not to the entry, so a
  break that had already run past that level produced a LONG whose take-profit
  sat BELOW its entry — 57 of 514 builder invocations, e.g. close 107.26 with
  a target of 101.01. The gatekeeper's `_entry_levels_valid` refused those
  signals so nothing traded, and the ORB regime is currently disabled in the
  shipped preset; but a builder should not emit a structurally invalid setup
  and rely on a downstream guard. It now rejects with
  `orb_measured_move_exhausted` when the target cannot clear
  `shared_entry.min_target_rr` from the current close — the same way sr_scalp
  rejects when its zone gap cannot pay for its stop. A re-run shows 0
  violations across 457 invocations of all seven builders, both sides.

- **Risk controls that failed open now say so.** *2026-09-18* —
  `open_risk_to_stops` skipped positions whose levels would not parse, which
  UNDER-counts open risk and makes the daily-loss projection more permissive;
  and `can_open` swallowed a strategy-params lookup failure, silently
  disabling the correlation concentration guard entirely (`max_group` -> 0).
  Both now log. A zero-quantity position stays silent — that is normal, not a
  data problem.

- **`_strategies/rvol.py`: three defects in the liquidity-profile module.**
  *2026-09-18* — six strategies route their volume gating and focus scoring
  through it and it had no direct coverage.
  - **`_symbol_set` mishandled every Mapping spelling.** It read `.values()`,
    so the natural YAML set `{AAPL: true, MSFT: true}` collapsed to the single
    token `TRUE` and silently discarded the symbols, while
    `{tech: [AAPL, MSFT]}` stringified the list into one bogus token. Now the
    key is the symbol when the value is scalar and the values are the symbols
    when the value is a container; nested structures flatten and the recursion
    is depth-bounded.
  - **A `rvol_score_floor` above `rvol_score_cap` silently returned a
    constant.** `min(cap, max(floor, raw))` with an inverted pair yields `cap`
    for every input, so the volume term stopped distinguishing a dead tape
    from a 5x surge and every `focus_score` built on it degenerated to a
    scaled day-change. The floor is now DROPPED rather than clamped to the cap
    (clamping leaves `floor == cap`, still a constant), and the
    misconfiguration is logged once per process — it fires from a per-symbol
    hot path.
  - **The hard-coded liquidity lists had drifted.** ORCL, PLTR, ARM, MU, ANET,
    QCOM, ADBE, NOW, PANW, MRVL and DELL were all absent, so they were gated
    at the full threshold while AAPL got an 80% relaxation — a 5x gap between
    comparably liquid mega caps. `IGV` was missing from the benchmark list
    while `SMH` and `XLK` were present. Lists refreshed, and
    `rvol_profile_for_symbol` now takes an optional `dollar_volume` so a
    symbol above `rvol_high_liquidity_dollar_volume` (default $1B) is treated
    as liquid regardless of the list. Threaded through all six consuming
    screeners, which already select `close` and `volume`.
  - 50 new tests, including one asserting no caller ever gates on the floored
    value — the separation that stops a benchmark ETF's 0.90 score floor from
    carrying it through its own volume gate on a dead tape.

- **`audit_logger` fallbacks could themselves raise.** *2026-09-18* — found by
  the new tests. `log_structured`'s except branch called `str(payload)`, which
  re-raises for a payload whose `__repr__` throws, so the safety net
  propagated into the caller — and the callers are the entry and exit flows
  (`ENTRY_CONTEXT` / `EXIT_CONTEXT`). `_json_ready`'s final `str(value)` had
  the same hazard, and it also feeds the sqlite position-metadata write. Both
  now degrade to `<unserializable {type}>` instead of throwing.
- **The engine now escalates persistent failures.** *2026-09-18* — the main
  loop backs off exponentially and never gives up, which is right, but a
  sustained outage during the management window left open positions unmanaged
  behind a throttled WARNING. After `runtime.error_escalation_cycles`
  consecutive failed cycles (default 10, roughly 10 minutes at the capped 60s
  backoff) the engine logs CRITICAL naming the exposed positions and the
  dashboard status carries the same alarm text. Re-announces on each multiple
  so a long outage does not scroll away. Set to 0 to disable.

- **`max_daily_loss` now projects open risk instead of comparing realized P&L
  alone.** *2026-09-18* — the gate blocked new entries once REALIZED P&L
  crossed the limit, and never flattened anything. With `max_positions` open
  at full risk at that moment, the day could finish at roughly twice the
  configured cap. `can_open` now subtracts `RiskManager.open_risk_to_stops`
  (each position's remaining loss to its CURRENT stop, so a stop trailed to
  breakeven contributes zero) before comparing. Set
  `risk.daily_loss_includes_open_risk: false` for the old realized-only
  comparison.
- **Per-day risk state survives a restart.** *2026-09-18* — `RiskState` was
  memory-only, so a crash or restart mid-session reset `realized_pnl` to 0.0
  and dropped every cooldown and same-level block. A bot restarted after
  losing most of its `max_daily_loss` came back believing the day was flat and
  could lose the limit again — and a restart is most likely exactly when
  something has already gone wrong. Open positions were always recovered from
  the broker; the counters that decide whether to open MORE were not. New
  `position_store.SessionRiskStateStore` keeps them in the same sqlite file as
  the reconcile metadata, keyed by ET session date so the daily reset is just
  the absence of a matching row. `RiskManager` takes the store at construction
  and restores before the first cycle; persistence failures log and never
  raise.

- **Entry sizing now accounts for slippage, and realized risk is reconciled
  after the fill.** *2026-09-18* — `qty` is computed before the order from the
  previewed limit price, but realized risk is `qty * |fill - stop|`, so an
  adverse fill risked more than `max_notional_per_trade *
  risk_per_trade_frac_of_notional`. `size_position` now takes a
  `slippage_allowance` that widens the SIZING distance only (never the actual
  stop), derived from the live spread via `RiskManager.entry_slippage_allowance`
  and capped at `entry_slippage_allowance_max_pct` of price.
  `RiskManager.realized_entry_risk` then reconciles what the trade actually
  risks against the budget; anything beyond `risk_overage_warn_frac` is logged
  and stamped on the position as `entry_risk_overage_frac`. Detection only on
  that half — the shares are already bought, so the sizing allowance is the
  preventive control.
  - Scope note: the exposure is bounded by whichever constraint sized the
    trade. The risk budget only binds when stop distance exceeds
    `budget / max_notional` as a fraction of price (0.800% on the top_tier
    preset); below that the notional cap decides the size and leaves large
    slack. The worst case is therefore a LOW-priced name with a stop just past
    that crossover (a $50 name on a 1% stop, where a 10c slip reaches ~20% over
    budget), not the tightest structural stops — at 0.11% of price the notional
    cap holds realized risk ~80% UNDER budget.
- **Equity entries re-validate their levels after the fill.** *2026-09-18* —
  the pre-order check ran against the previewed price, so a fill that slipped
  through its own stop left a position whose entry was already past its stop
  with no warning (`initial_risk` uses `abs()`, so it still read positive and
  the stop simply fired on the next management cycle). The options path had
  always revalidated here; equities now match it, falling back to
  `default_stop_pct` / `default_target_pct` distances from the actual fill
  rather than orphaning the position or keeping levels the fill invalidated.
- **Entry slippage is now watched, not just recorded.** *2026-09-18* —
  `entry_slippage_pct` reached `paper_account` and the end-of-day report and
  nothing else, so a routing or liquidity degradation surfaced only if someone
  diffed reports by hand. Breaches of `entry_slippage_warn_pct` are logged and
  flagged on the position. `realized_entry_risk`, `entry_risk_budget` and
  `entry_risk_overage_frac` are carried on `TradeRecord` and serialized into
  the session report alongside it.
- **`_in_orb_window` is bounded at both ends.** *2026-09-18* — it read
  `now <= orb_end_time` with no lower bound and no check that the ORB regime
  existed, so the seven `orb_bypass_*` relaxations (HTF bias, structure, S/R,
  exhaustion, side decision, relative strength, screener bias) applied to
  entries with no ORB thesis behind them. Harmless on the RTH top_tier preset
  (09:45 is its first entry), but live on `small_cap_squeeze`, which sets
  `equity_session_indicator_window: extended` and `disable_orb_regime: true`:
  its entries from 08:05 to 10:05 — the majority of an 08:05-11:50 window —
  ran with those gates off. Now `[opening-range end, orb_end]`, and always
  `False` when `disable_orb_regime` is set.
- **`daily_stats.compute_adr_pct` dropped the window's first bar.** Its
  `prev_close` is NaN and the row-wise max skips NaN rather than propagating
  it, so that bar contributed a high-low value instead of a true range and a
  20-day lookback returned 21 samples.

### Added

- **Broker-side bracket orders: the entry, stop, and target go out as one
  Schwab first-triggers-OCO order.** *2026-07-27* — the protective exit now
  rests AT THE BROKER instead of waiting for the engine's management poll to
  observe the level and fire a marketable limit. At `quote_poll_seconds: 6`
  that removed up to ~6s of latency on exactly the fast moves where it costs
  most. Opt-in via `execution.bracket_orders_enabled` (default `false`), so
  every existing preset keeps today's fully engine-managed exits.
  - **Spec** (`execution.py::build_bracket_order`): a `TRIGGER` parent whose
    child OCO carries a `LIMIT` target and a `STOP`/`STOP_LIMIT` protective
    stop. A lone stop is attached directly rather than wrapped in a
    single-child OCO. Levels round to valid ticks (penny at/above $1) biased
    so a stop never lands *tighter* than intended and a target never lands
    further away — sub-penny prices are rejected outright on stop legs.
  - **`execution.bracket_legs`** — `stop_and_target` rests both;
    **`stop_only`** rests only the stop and leaves the target engine-side.
    `stop_only` is *required* with `trade_management_mode: adaptive_ladder`
    and is enforced at config load: a resting target limit fills through
    `adaptive_ladder_suppress_target_exit` (the ladder declining a target tag
    so it can roll to the next rung) and through the final-rung runner
    (`target_price` cleared to `None`), so the ladder could never extend.
  - **`execution.bracket_sync_mode`** — `static` submits once and never
    touches the order; `replace` keeps the engine managing and pushes every
    stop/target move onto the resting child via `replace_order`, debounced by
    `bracket_replace_min_price_delta` so a per-cycle trail ratchet cannot burn
    the Schwab rate budget. `static` is refused at load for the ratcheting
    management modes, which would otherwise leave the broker on the entry-time
    stop for the life of the trade.
  - **Ownership split** — `RiskManager.update_position` suppresses only the
    exits actually resting at the broker, keyed on the live child order ids
    (not the config) so a bracket that failed to establish correctly falls
    back to engine exits. Engine-only exits (peak giveback, trailing, time
    stop, CHoCH, force flatten) still fire, and `PositionManager` cancels the
    resting orders **before** marketing out. A failed cancel *defers* the exit
    rather than double-filling — the protective stop is still resting, so the
    position is not left unguarded.
  - **Phantom-position reconciliation** — `_reconcile_bracket_fills` runs at
    the TOP of `manage_positions`, booking any exit the broker already
    executed before anything can manage or exit a position that no longer
    exists. Uses one `account_orders` call for all positions, not per-position
    `order_details`: at 4 positions and `loop_sleep_seconds: 2.0` the latter
    would be 120 req/min, the entire Schwab budget. An unreadable broker
    response is treated as "unknown", never as "nothing filled".
  - **Short-flip guard** — children are submitted for the *requested* entry
    quantity, so a partial fill leaves an oversized resting exit that would
    take a long-only strategy net short when it triggers. The partial path
    cancels the parent first (so the resize target cannot move underneath),
    then replaces both children at the filled quantity; if the children never
    materialised, standalone protection is submitted for the shares actually
    held.
  - **Adoption** — `ensure_position_protected` adopts still-working children
    and submits fresh protection only when they are dead, so broker entry
    recovery and startup restore can never stack a second protective order.
    Startup restore also rewrites stale `bracket` metadata: left alone, a dict
    whose children died overnight would suppress the engine's stop exit for a
    position with nothing resting at the broker.
  - **Dry run** keeps exits engine-side and stamps the intended bracket for
    inspection. Live fills at the resting limit will beat these poll-priced
    exits, so dry-run *understates* the benefit.
  - No reprice loop on a bracketed entry: cancel/replace churn on a `TRIGGER`
    parent with live children is how brackets get orphaned, and an entry
    needing several reprices has already left the level it was built on.
  - 40 tests in `tests/test_bracket_orders.py`.

### Fixed

- **Same-level retry block measured from the wrong price and ignored winners.**
  *2026-07-31* — `StopoutRecord` is now `RecentExitRecord` and the block keys off
  the prior ENTRY price, the level actually being re-tried, instead of the exit.
  A stopped-out SHORT exits roughly 1R ABOVE its entry, so re-entering the same
  level always sat ~1R from that exit and cleared any sane threshold. AAPL
  2026-07-30 took FIVE shorts inside $0.37 (331.46 / 331.57 / 331.66 / 331.78 /
  331.83): consecutive entries were 0.05-0.12 apart (0.1-0.25 ATR) while the
  exit-referenced distances were 0.50-1.02 — four to ten times larger. Only one
  of the five was ever blocked, and widening `same_level_block_atr_mult` to 1.5
  the day before had not helped because the reference point was wrong.
  Additionally, every exit is now recorded rather than losses only: the
  loser-only rule left a re-entry after a WIN completely unchecked, which is how
  that run alternated W/L/W/L/L through one price for three hours. All four
  `position_manager` call sites pass `entry_price`; a test asserts none can
  regress.

- **Low-tier peak-giveback retained almost nothing.** *2026-07-31* — with the
  partial-breakeven tier disabled the day before, `peak_giveback_low_tier`
  became the earliest ratchet, and at `giveback_frac: 0.7` it kept only 30% of
  peak. NEM 2026-07-31 peaked 0.85R and booked +0.17R. Across 07-30/31 winners
  realised 39% of their peak (avg +0.58R against a 1.39R median MFE) while
  losers ran -1.23R, which needs a 68% win rate to break even; 55% was achieved.
  `peak_giveback_low_tier_giveback_frac` 0.7 -> 0.45 retains 55%, bridging to
  the main tier's 0.65 retain at 1R+.

- **Side-decision gate rejected shorts throughout a tech selloff.** *2026-07-29*
  - Over 2026-07-27..29 the tech universe fell 4-12% (AMD -12.3%, NVDA -6.2%,
    INTC -4.6%, TSLA -4.4%, XLK -3.9%) and `top_tier_adaptive` won zero shorts.
    `side_undecided` was the single largest blocker on those names — 3,506 of
    12,935 decision rows — ahead of every regime and quality gate.
  - Two defects in `_decide_side`. **(a)** `side_decision_min_votes` was an
    ABSOLUTE count against a variable number of voters, while `recent` and
    `vwap` both carry neutral dead-bands and abstain ~35% of the time; a
    unanimous 2-0 read therefore failed a 3-vote bar. **(b)** the last-3-bars
    colour signal had no neutral band (`greens >= 2` LONG else SHORT), so with
    three bars it always voted, and in a downtrend the constant two-green
    bounce bars voted LONG against the trend while carrying the same weight as
    the EMA structure — it split 1,021 L / 1,053 S, a coin flip.
  - Net effect: the reliably-achievable SHORT tally in a downtrend was 2
    (vwap + ema), so requiring 3 only admitted shorts once price was already
    making a new low — at local exhaustion, right before the bounce that swept
    the stop. This is the same root cause as the "entered late, stopped on the
    reversal" pattern (XOM 07-29 entered at 98.7% of its leg).
  - `side_decision_min_votes` is REMOVED. The threshold is now a majority of
    the signals that actually voted:
    `required = max(side_decision_min_agreeing, ceil(participating × side_decision_majority_frac))`,
    defaults `2` and `0.6`. The 3-bar signal votes only when unanimous (3 green
    / 0 green) and otherwise abstains. Replaying the 3,506 undecided rows:
    07-28 (bounce, XLK +0.23%) 49% LONG / 19% SHORT; 07-29 (selloff,
    XLK -2.10%) 37% LONG / 38% SHORT — shorts go from unavailable to available
    on the down day, with the long-lean preserved on the bounce day. A lone
    structural signal still abstains (`min_agreeing` floor).

- **`min_pullback_score: 4.0` was above the regime's reachable ceiling.**
  *2026-07-29* — raised from 3.5 two days earlier on a 48-trade sample. Live
  pullback scores top out at 3.5, so the floor silently disabled the regime
  (185 of 185 otherwise-qualifying tech shorts blocked) — the same failure the
  config comment warns about for `min_sr_scalp_score`. Reverted to 3.5.
  `min_trend_score` 4.5 -> 4.0 as well: 4.5 blocked 384 of the 631 tech shorts
  that cleared the old floor. A regression test now asserts both floors sit
  below their observed ceilings.

- **sr_scalp built stops inside the noise band.** *2026-07-29* — zone geometry
  can park the stop a fraction of an ATR from entry regardless of how wide the
  tape is. On 07-28 META chopped 11.9 ATR between 13:50-14:50 while sr_scalp
  shorted the FLOOR of that band three times with stops 1.09-2.70 ATR out; 63%
  of the window's bars traded above those stops. All three needed 2.7-4.3 ATR
  to survive and all three resolved in the trade's direction after stopping
  out. `shared_entry.min_stop_atr_mult` does not cover this — it bounds only
  the refinement passes, never a builder's own stop. New
  `sr_scalp_min_stop_atr_mult` (default 2.5) floors the builder stop, and when
  widening drops R:R below `shared_entry.min_target_rr` the setup is rejected
  outright, since sr_scalp's reward is capped by the opposing zone.

- **Partial-breakeven tier scratched working trades.** *2026-07-29* — it moved
  the stop to entry + `adaptive_partial_breakeven_offset_r` (0.0 = exactly
  entry) once `max_favorable_r` crossed 0.5R. Median trade MFE is ~0.5R, so it
  armed at the median trade's peak and a normal retrace then scratched it.
  NFLX 07-28 SHORT: a 5.9-ATR stop it never came near (needed 1.1), ratcheted
  to entry at a 0.52R peak, shaken out for -$2.18 — then ran to +2.47R. The
  tier existed to bridge the gap between the trail and a 1.0R breakeven; with
  breakeven now at 0.60R that gap is gone and the two sat 0.1R apart doing the
  same thing. Disabled (`adaptive_partial_breakeven_rr: null`).

- **Same-level re-entry block was too narrow to catch repeats.** *2026-07-29* —
  `same_level_block_atr_mult: 0.3` is $0.14 on META, so the three 07-28 SHORTs
  at 593.05 / 593.82 / 593.17 (13:55, 14:20, 14:39, all stopped at ~594.3) read
  as three different levels, and the 12-minute cooldown was cleared by the
  19-25 minute gaps. Widened to 1.5 ATR, which folds that pocket into one level.

- **`BaseStrategy._position_r_multiple` is now a `@staticmethod`.**
  *2026-07-27* — it never referenced `self`. Matches the neighbouring
  `_frame_atr14`; both call sites are unchanged.

- **Stop-refinement had no floor and silently overrode every builder's
  `default_stop_pct` backstop.** *2026-07-27*
  - `_refine_bullish_sr_levels` / `_refine_bearish_sr_levels` /
    `_refine_bullish_technical_levels` / `_refine_bearish_technical_levels`
    each moved the stop TOWARD entry whenever a support/resistance level or
    trendline sat inside the strategy's structural stop, with no lower bound.
    A level a few cents from entry produced a few-cent stop. `min_target_rr`
    could not catch it: tightening a stop RAISES reward/risk, so the R:R guard
    that protects the *target* cap never binds on the stop side.
  - Measured over 2026-05-12..29 on `top_tier_adaptive`: **46 of 57 entries had
    their stop pinned exactly to `nearest_support - level_buffer`**, a median 2x
    (worst 10x — META 2026-05-26 got 0.11% of price against the 1.0%
    `default_stop_pct` floor) tighter than the builder intended, parking the
    stop on the single price most likely to be swept.
  - New `shared_entry.min_stop_atr_mult` (default `1.5`) floors the refined
    stop in ATR14 units, via `BaseStrategy._clamp_refined_stop`. An over-tight
    proposal is *clamped back* to the floor rather than discarded: discarding
    reverts to the flat `default_stop_pct`, which on a quiet symbol is enormous
    in ATR terms (one logged case reached 11 ATR) and would push R out far
    enough that nothing priced in R — breakeven, profit-lock, runner,
    peak-giveback, `discretionary_exit_min_r` — could ever arm. The clamp never
    widens past the incoming stop, so builders that deliberately choose a
    tighter stop (range / sr_scalp / momentum) keep it.
  - Replayed against all 57 logged entries: 9 stops adjusted, all to exactly
    1.5 ATR; median stop width unchanged at 3.87 ATR.
  - The two SR helpers now take the bar `frame` (to read ATR14); all 16 call
    sites across 9 strategies updated.

- **Discretionary exits carried no R condition and were displacing the stop.**
  *2026-07-27*
  - The bias-based structure exits, every branch of `_technical_exit_signal`
    (trendline / channel / bollinger / anchored-VWAP) and the S/R break exits
    are pattern reads on the tape gated only by grace windows and tape
    confirmation. On a trade still hovering around entry they acted as an
    arbitrary tightened stop.
  - Over 2026-05-12..29 they closed **19 of 48 `top_tier_adaptive` trades at a
    median MFE of 0.09-0.29R for -$831 combined**, and among trades whose stop
    sat beyond 4 ATR, **0 of 22 ever reached that stop** — one of these got
    there first, every time.
  - New `shared_exit.discretionary_exit_min_r` (default `0.5`) gates the family
    on open profit in initial-risk R units, via
    `BaseStrategy._discretionary_exit_allowed` / `_position_r_multiple`. R is
    measured against `metadata['initial_stop_price']`, not the trailing
    `position.stop_price`, so it does not drift as management moves the stop.
    CHoCH exits stay exempt — a true change-of-character is a reversal signal,
    not noise.
  - Counterfactual replay of the 21 suppressed exits against the archived 1m
    bars: **-$722 under the full post-fix ladder vs -$859 actually booked
    (+$137)**, and -$874 under a deliberately pessimistic bound that caps all
    upside at the moment the gate opens. 7 of the 21 go on to hit a full stop —
    the discretionary exits were partly doing useful work, so the gain is
    materially smaller than the -$831 those exits booked. A threshold sweep
    (0.25 / 0.5 / 0.75 / 1.0R) favours the gate at every non-zero value under
    the full-ladder replay, but one-trade differences swing the ranking at
    n=22, so the sample cannot tune it further.

- **`sector_index_map` could silently disable five regimes for a whole sector.**
  *2026-07-27*
  - `TopTierAdaptiveStrategy.active_watchlist` streams bars for `index_symbols`
    and nothing else, while `_indices_for_symbol` resolves a candidate's sector
    through `sector_index_map`. When the two disagreed, `_index_confirms` got
    `None` bars for every mapped ETF, fell through its loop and returned False —
    permanently blocking every symbol in that sector from the five
    index-confirmed regimes (trend / pullback / vol_squeeze / momentum /
    vwap_reclaim). The only symptom was a `..._index_not_confirmed` skip line,
    indistinguishable from the index genuinely disagreeing.
  - `config.load_config` now runs `_validate_sector_index_map` and fails loudly
    at load time. Only sectors that actually have members are checked — mapping
    a sector you have not populated yet is harmless, and presets routinely carry
    a full 11-GICS map against a narrower traded universe.

- **README `top_tier_adaptive` defaults table had drifted from the manifest.**
  *2026-07-27* — `min_bars` 60→150, `ltf_minutes` 5→1 (the 1m-LTF migration
  landed 2026-05-29 but the table kept the 5m-era numbers), `min_ltf_bars`
  15→120, `min_sr_scalp_score` 3.5→3.0.

### Changed

- **Dependency pins bumped to latest.** *2026-07-27*
  - `schwabdev` 3.0.4 → 3.0.5, `tradingview-screener` 3.2.0 → 3.2.1,
    `pandas` 2.3.3 → **3.0.5**, `numpy` 2.4.4 → 2.4.6, `TA-Lib` 0.6.8 → **0.7.1**.
    `PyYAML` stays at 6.0.3 (already latest). `requires-python = ">=3.11"` is
    unchanged — pandas 3 and numpy both floor at 3.11.
  - pandas 3 makes **Copy-on-Write the default**, the usual breaking point for
    this upgrade. The package is CoW-safe by construction: zero `inplace=True`
    across the codebase, no chained DataFrame assignment, no `applymap` /
    `DataFrame.append` / `.ix` / `iteritems`. No migration was needed.
  - Verified numerically rather than by test pass alone. The session archive's
    `bars/1m/*.csv` carry indicator columns computed under the OLD stack
    (pandas 2.3.3 + TA-Lib 0.6.8), so they serve as a regression oracle:
    recomputing `vwap / ema9 / ema20 / atr14 / rsi14 / adx14 / ±DI /
    bollinger / obv / ret5 / ret15` from raw OHLCV under the new stack over
    8 symbols × 15 columns (~8,100 bars) gives a worst relative drift of
    **9.2e-13**, confined to the `ret5`/`ret15` percentage columns — float64
    round-off, not a behaviour change. This covers the TA-Lib bump too.
  - schwabdev surface re-checked against the string-dispatch call sites: all 9
    methods the bot invokes (`account_details`, `account_orders`,
    `cancel_order`, `linked_accounts`, `option_chains`, `order_details`,
    `place_order`, `price_history`, `quote`) exist in 3.0.5, and
    `Client.__init__` still accepts `open_browser_for_auth`, which
    `SchwabConfig` depends on. `Stream.chart_equity/send/start/stop` intact.
  - 471 tests pass, `pip check` clean, all 19 shipped configs load and build
    their strategy. Not yet exercised against the live broker or the live
    TradingView endpoint — the beta dry-run this file's header calls for still
    applies before prod.

- **`config.small_cap_squeeze.yaml` declares the two new shared knobs.**
  *2026-07-27* — `min_target_rr: 1.0`, `min_stop_atr_mult: 1.5` and
  `discretionary_exit_min_r: 0.5` are now explicit, matching this preset's
  fully-explicit convention. Functionally a no-op — the dataclass defaults
  already supplied those values — but the preset should show what it runs. Its
  adaptive ladder is deliberately left alone (breakeven 1.2 / lock 1.8 /
  lock-stop 1.0 / runner 1.3): those were tuned UP for small-caps, the opposite
  direction from the top_tier retune, which was calibrated on large-cap tape.

- **`top_tier_adaptive` regime mix and profit ladder retuned from measured
  excursion.** *2026-07-27*
  - Measuring excursion in ATR units (independent of stop placement, so the
    regimes are comparable) over 2026-05-12..29: `vol_squeeze` MFE 1.45 / MAE
    0.75 ATR = **1.93** edge; `pullback` 1.65 / 2.04 = **0.81**; `trend` 1.79 /
    2.36 = **0.76**. The two sub-1.0 regimes were carrying 60% of trade volume
    (pullback alone 24 of 48). `min_trend_score` 3.5 → **4.5**,
    `min_pullback_score` 3.5 → **4.0**; `vol_squeeze` untouched at 4.0. Sized
    against the score ceilings (`_score_trend` caps at 6.0, `_score_pullback` at
    5.0, `bias_penalty` subtracts at most 1.0) so neither becomes unreachable —
    cf. `min_sr_scalp_score`, which once sat above its own ceiling and fired
    zero times ever. This cuts VOLUME, not per-trade edge: `regime_score` was
    not stamped into signal metadata during those sessions, so the score/outcome
    curve is unknown. It is stamped now, so the next dry-run can set these from
    data.
  - Every profit-protection trigger sat at or above 1.0R while **79% of trades
    (38 of 48) never reached 1R at all**, so the ladder almost never armed and
    sub-1R trades ran unprotected back to the stop (GOOG 2026-05-14 peaked at
    0.97R and closed at -0.01R; INTC 2026-05-26 0.80R → -0.02R). Excursion is
    MFE p25 0.43 / median 1.55 / p75 3.05 ATR against a typical ~3 ATR stop, so
    the median trade peaks near 0.5R. `adaptive_breakeven_rr` 1.00 → **0.60**,
    `adaptive_profit_lock_rr` 1.20 → **0.85**, `adaptive_runner_trigger_rr`
    1.10 → **0.90** (`adaptive_profit_lock_stop_rr` unchanged at 0.45).
    Breakeven now arms on 40% of trades instead of 21%. Ordering is deliberate:
    `discretionary_exit_min_r` 0.50 < breakeven 0.60 <
    `peak_giveback_low_tier_min_r` 0.70 < profit-lock 0.85 < runner 0.90 <
    `peak_giveback_min_r` 1.00.

### Removed

- **Dead config parameters.** *2026-07-27*
  - `min_score_gap` from `top_tier_adaptive` (manifest, preset, README, strategy
    README). The primary-vs-fallback selection paths it gated were collapsed
    into the flat score-ordered build queue on 2026-05-12; the code had carried
    a comment saying it was "silently ignored" ever since. Still live and read
    by `zero_dte_etf_options` / `zero_dte_etf_long_options`, which are untouched.
  - `range_target_rr` from `top_tier_adaptive` and `small_cap_squeeze` — no code
    in the package ever read it. `_build_range_signal` targets
    `range_high - buffer`, never an R multiple.

### Added

- **`small_cap_squeeze` strategy — long-only small-cap squeeze.** *2026-05-30*
  - New plugin (`_strategies/small_cap_squeeze/`): a long-only, multi-regime
    small-cap "squeeze" strategy. A dynamic TradingView screener replaces the
    fixed tradable list — float 400K-20M, change-from-open ≥5%, RVOL ≥2.0,
    volume ≥5M, price $2-20, no market-cap cap (float + price define the small
    bias). The canonical `close`/`change_from_open`/`volume` fields auto-resolve
    to premarket variants pre-09:30; float via `float_shares_outstanding_current`.
    Ranked by gap% × clipped RVOL, bias always LONG. Watchlist mode
    `premarket_lock_rth_live`: premarket gappers are sticky-locked (no pre-open
    churn), then at 09:30 a live RTH re-screen is unioned with the locked set
    (faded gappers kept warm for a VWAP reclaim), capped to max_candidates.
  - Thin subclass of `TopTierAdaptiveStrategy` (sets `strategy_name` only) — all
    behavior is the shared engine, driven by config. Regimes (narrowed 2026-06-02
    after two dry-runs): trend / momentum + opt-in `vwap_reclaim` ONLY — pullback /
    range / vol_squeeze / ORB / sr_scalp off (pullback bled both runs; the mean-
    reversion / breakout regimes don't fit a squeeze-continuation thesis). With ORB
    off the opening carve-out is removed, so the open trades the mix continuously
    08:05-11:50. No index / sector / relative-strength
    confirmation. 1m LTF with native indicators (`ltf_indicator_span_scale: 1`).
    Long-only via `risk.allow_short: false`. Premarket/extended-hours eligible.
  - Shipped preset `config.small_cap_squeeze.yaml` is a fully-explicit preset
    (every engine section declared, mirroring `config.top_tier_adaptive.yaml`).
    Beyond the strategy params it tunes: widened execution marketable-limit
    buffers (wider/faster small-cap books — live-fill only, dry-run fills at the
    natural price), zeroed the `technical_levels` soft extension penalties (a
    squeezer is always extended — the matching hard gates are off too), and
    looser SR entry clearance for buy-through-resistance breakouts. The tuning is
    a starting point pending a beta dry-run.
  - New `tests/test_small_cap_squeeze.py` (registration, config, allowed_regimes,
    vwap_reclaim scorer).

- **Shared engine: opt-in `vwap_reclaim` regime + two base flags.** *2026-05-30*
  - **`vwap_reclaim` regime** (`enable_vwap_reclaim_regime`, default `false` →
    `top_tier_adaptive` byte-for-byte unchanged). A long re-entry when price dips
    below session VWAP (a flush that shakes out weak longs) then reclaims it on a
    volume pop — the squeeze re-igniting. Catches the entry that trend (needs
    `close>VWAP` & `ema9>ema20`), pullback (needs to hold above ema20), and
    momentum (needs a new N-bar high) all miss. New `_score_vwap_reclaim` +
    `_build_vwap_reclaim_signal` (stop below the flush low, target rides toward
    the session high floored to `vwap_reclaim_target_rr`); exempt from the
    confirmation-bar gate (it enters on the reclaim bar by design). Knobs:
    `min_vwap_reclaim_score`, `vwap_reclaim_lookback_bars`,
    `vwap_reclaim_min_volume_ratio`, `vwap_reclaim_buffer_pct`,
    `vwap_reclaim_target_rr`.
  - **`disable_orb_regime`** (default `false`) — drops the ORB regime AND its
    opening-range carve-out, so the normal regime mix runs continuously from the
    open. Distinct from `disable_orb_window` (which skips the opening window and
    starts at `orb_end_time`).
  - **`extended_hours_tradable_all`** (default `false`) — treat every screened
    symbol as extended-hours eligible instead of gating on the
    `extended_hours_tradable` sublist. For dynamic-universe strategies where a
    hand-listed sublist can't enumerate the names.
  - All three default off, so `top_tier_adaptive` and every other preset are
    unchanged.

- **top_tier_adaptive: 1-minute LTF with horizon-preserving indicator scaling.** *2026-05-29*
  - The trend/pullback **LTF moved from 5m to 1m** (`ltf_minutes: 5 → 1`) so
    entries/exits act on the freshest 1m close instead of waiting up to 5 min
    for a 5m bar to print. **Behavior is preserved**, not changed: the 1m LTF's
    indicators are stretched ×5 so their wall-clock horizons match the old 5m
    frame.
  - New shared capability — `add_indicators(frame, *, span_scale=1.0)` multiplies
    every bar-count lookback (ema9→45, ema20→100, bb→100, atr14→70, ±DI/adx→70,
    obv_ema→100, rsi14→70, ret5→25, ret15→75). **Default `1.0` is byte-for-byte
    unchanged** for every existing caller. Threaded through
    `ensure_standard_indicator_frame` → `DataFeed.get_merged` (the enriched
    cache is now keyed by `span_scale`, so top_tier's scaled 1m frame never
    collides with the shared `span_scale=1.0` frame the engine bars / dashboard /
    other strategies read) → `_resampled_frame`. New `TestSpanScale`.
  - top_tier params: `ltf_indicator_span_scale: 5`; bar-count LTF lookbacks
    scaled to match (`pullback_lookback_bars` 5→25, `side_decision_recent_lookback_bars`
    6→30); warmup gates bumped (`min_bars` 90→150, `min_ltf_bars` 15→120,
    `history.required_bars` 90→150). The `range`/`vol_squeeze`/`momentum`
    regimes, `technical_levels`, and chart-pattern detection read the **base 1m
    frame** (unchanged by the switch), so their lookbacks were left as-is; the
    structure-pivot frame stays 5m. Every ATR-mult/pct/score threshold is
    unchanged (the whole point of preserving horizons).
  - Dashboard: the compact (LTF) chart redraws its EMA lines at the scaled
    spans (`ltf_ema_fast_span: 45` / `ltf_ema_slow_span: 100`, defaulting to
    base×scale), mirroring the existing HTF-chart EMA parity, so the chart
    matches what the bot evaluates.

- **top_tier_adaptive: true Opening Range Breakout (ORB) regime.** *2026-05-29*
  - The opening window used to run the **trend** regime with ~11
    `orb_bypass_*` flags loosening its filters — there was no opening range
    computed at all (the "breakout" was a rolling 5-bar-high). Replaced with a
    real ORB regime:
    - The **opening range** = high/low of the first `orb_range_minutes` of RTH
      (default 15 → 09:30-09:45), computed on the raw 1m frame.
    - **No entries while the range forms** (09:30 → range-end); from range-end
      → `orb_end_time` the ORB regime is the ONLY regime, trading a break.
    - **Entry:** close breaks the range edge by `orb_breakout_buffer_atr_mult`
      × ATR. **Stop:** the OPPOSITE range edge. **Target:** a measured move
      (range height × `orb_target_range_mult`, default 1.5×), then capped to
      HTF levels / floored to min R:R by the shared finalize path.
    - Range size is sanity-bounded (`orb_min_range_atr_mult` /
      `orb_max_range_atr_mult`) so noise ranges and untradeable wide ranges
      are skipped. Score floor `min_orb_score` (default 3.5).
  - New scorer `_score_orb` + builder `_build_orb_signal` + helper
    `_opening_range`; wired into the regime scoring/selection/dispatch and
    `_allowed_regimes` (ORB window now returns `{"orb"}`, not `{"trend"}`).
    The `orb_bypass_*` flags still apply (they loosen the shared finalize
    filters that are stale at the open) and `disable_orb_window` still skips
    the whole open. Regression tests in `TestORBRegime`.

- **top_tier_adaptive extended-hours trading (07:00-20:00 ET).** *2026-05-28*
  - New opt-in `runtime.equity_session_indicator_window: "rth" | "extended"`
    (default `"rth"`). In `"extended"`, `add_indicators` anchors the
    per-session VWAP/EMA reset to the 07:00-20:00 equity-stream window (via
    `is_equity_stream_session`) instead of RTH 09:30-16:00, so pre/post-market
    bars carry meaningful session indicators. Every RTH-only strategy/preset
    is byte-for-byte unchanged (verified: default mode still resets VWAP at
    09:30; 219+ regression tests green).
  - `_session_open_price` gained a `session_start` anchor; top_tier's
    `day_strength` bias keys off the 07:00 open in extended mode (matching the
    VWAP reset) via `_day_strength_session_open`.
  - `_allowed_regimes` opens the full non-ORB regime mix pre-market (<09:30)
    in extended mode; ORB stays RTH-anchored (range-end → orb_end). After-RTH
    is covered by the afternoon window once `no_new_entries_after` is extended.
  - Extended-hours universe gate (`params.extended_hours_tradable`): outside
    RTH only the configured liquid names may enter (thinner names trade RTH
    only); empty list => no extended-hours entries.
  - The shipped preset `config.top_tier_adaptive.yaml` defaults to **RTH-only**
    (`equity_session_indicator_window: rth`, entry window `09:45-15:00` — entries
    open when the ORB regime can first fire — management `09:30-15:55`,
    `no_new_entries_after: 15:00`). The `extended_hours_tradable` list is retained
    but inactive in RTH mode. To run extended hours, set the mode to `extended`
    and widen the entry/management/screener windows (e.g. 07:00-19:30/19:55/19:45,
    `no_new_entries_after: 19:30`).

### Removed

- **top_tier_adaptive: dead `orb_bypass_*` params (5).** *2026-05-29* Now that
  the ORB window runs the dedicated `orb` regime (not trend-with-bypasses),
  `orb_bypass_index_confirmation`, `orb_bypass_entry_confirmation_bar`,
  `orb_bypass_stretched_filter`, `orb_bypass_oversized_entry_bar`, and
  `orb_bypass_tech_bias_contradiction` were dead — their gates are keyed to
  regime sets (`{trend,pullback,vol_squeeze,momentum}` / `{...,sr_scalp}`) that
  exclude `orb`, and those regimes no longer run in the ORB window, so the
  bypasses never fired. Removed from `strategy.py` (vars + the `and not
  orb_*_bypass` clauses), `config.top_tier_adaptive.yaml`, `manifest.json`, and
  the README. Surviving ORB bypasses (`htf_bias`, `exhaustion`,
  `structure_entry`, `sr_entry`, `screener_bias`, `side_decision`,
  `relative_strength`) DO apply to the ORB regime and were kept.

### Fixed

- **Dashboard HTF chart now draws the EMA 50/200 the bot actually uses (top_tier_adaptive).** *2026-05-28*
  - The HTF trend context computes EMA 50/200 on the 15m frame
    (`_default_htf_context_for_score` hardcodes `ema_fast_span=50,
    ema_slow_span=200`), but the preset didn't set
    `htf_ema_fast_span`/`htf_ema_slow_span`, so the dashboard HTF chart fell
    back to ema9/ema20 (fast EMAs of the 15m bars) — misrepresenting the HTF
    trend the bot evaluates. Added `htf_ema_fast_span: 50` /
    `htf_ema_slow_span: 200` to the preset so the HTF chart EMA override
    (`DashboardCache.chart_payload`) renders 50/200. Chart-only — entry logic
    is unchanged (top_tier's HTF direction is structure-based via
    `require_htf_bias_alignment`, not an EMA cross; the 50/200 `htf_trend_bias`
    is recorded as context but not gated on).

- **Dashboard TradingView ticker deep-links were broken for top_tier_adaptive.** *2026-05-28*
  - Ticker chips link to `tradingview.com/symbols/<EXCHANGE>-<SYMBOL>/`.
    Two causes left `<EXCHANGE>` wrong for the entire top_tier universe:
    - `top_tier_adaptive/screener.py` was the ONLY equity screener that did
      not `select("exchange")`, so its candidates carried no exchange and
      the dashboard fell back to the live Schwab quote.
    - `dashboard_quote_exchange` read Schwab's single-letter `exchange`
      code (`"q"`/`"n"`/`"a"`/`"p"`) BEFORE the full `exchangeName`
      (`"NASDAQ"`/`"NYSE"`). Single letters aren't valid TradingView
      exchanges (not in `_EXCHANGE_ALIASES`), so the builder produced
      `symbols/N-XOM/` (404) for NYSE names; NASDAQ names (`"q"` → `''`
      in the front-end guard) got no link at all.
  - Fix: top_tier screener now selects `exchange` (matching every other
    equity screener — `_row_metadata` carries it straight into
    `metadata["exchange"]`); `dashboard_quote_exchange` now prefers the
    full `exchangeName`/`primaryExchangeName` over the single-letter code,
    falling back to the short code only as a last resort.
  - Verified: `n+NYSE→NYSE`, `q+NASDAQ→NASDAQ`, `p+NYSE Arca→AMEX`,
    `a+NYSE American→AMEX`, `q+"NASDAQ Global Select"→NASDAQ`; simulated
    front-end URLs resolve to `symbols/NYSE-XOM/` and `symbols/NASDAQ-NVDA/`.
    New regression coverage in
    `tests/test_bug_regressions.py::TestTradingViewExchangeLinks2026_05_28`.

- **Dashboard structure overlay now mirrors the strategy's LTF structure params.** *2026-05-27 PM*
  - `DashboardCache.current_structure_overlay` built its CHoCH/BOS/EQH/EQL
    annotation with base params (`pivot_span`, no `min_pivot_gap_bars`,
    base `pct_tolerance`) regardless of timeframe, so after Fix B/D the
    LTF chart showed denser, noisier pivots than the bot actually acts on.
  - Now, when the overlay's display timeframe equals the strategy's
    effective LTF structure timeframe (`structure_ltf_timeframe_minutes`,
    falling back to `params.ltf_minutes`), it applies the same LTF
    overrides the strategy uses: `structure_ltf_pivot_span`, the 0.60x
    `pct_tolerance`, and `structure_min_pivot_gap_bars`. HTF / other
    timeframes keep the original base-param behavior unchanged. Mirrors
    the existing "keep the chart faithful to the strategy" precedent (the
    HTF-EMA override in the chart payload).
  - Verified: on NVDA 5/27 the 5m overlay bias now matches the bot's LTF
    structure read; the 15m overlay is byte-for-byte unchanged. Inherent
    residual: a 1m chart can't match (the bot no longer computes 1m
    structure under Fix D) — only the LTF (5m) and HTF (15m) charts do.

### Changed

- **Market structure: minimum pivot-gap filter (Fix B) + LTF-frame resampling (Fix D).** *2026-05-27 PM*
  - The pivot detector (`_reduced_pivots`) merged consecutive same-kind
    pivots but had NO rule against an alternating H↔L registering 1-2
    bars apart. On the raw 1m LTF structure frame with a 2-bar fractal,
    that produced a new "swing" every ~5 min (≈⅓ of pivots within 2 min
    of each other on NVDA), churning the HH/LH/EQH/LL/HL/EQL labels and
    the BOS/CHoCH/structure-exit signals keyed on them. The swings were
    genuine (median 1.9-5.2 ATR), so the problem was temporal density,
    not amplitude.
  - **Fix B** — new `structure_min_pivot_gap_bars` (default `0` = off).
    When > 0, an alternating pivot closer than N bars to the prior kept
    pivot is skipped as noise within the current leg. Added to
    `analyze_market_structure` and threaded from `_structure_context`.
  - **Fix D** — new `structure_ltf_timeframe_minutes` (default `0` = off).
    When > 0, `_structure_context` resamples the LTF structure frame to
    that timeframe before pivot analysis, so structure tracks the bars
    the strategy trades (params.ltf_minutes) instead of the 1m stream.
    Entry, exit, and HTF-alignment paths all pick it up.
  - Both default OFF, so every strategy that doesn't set them keeps the
    exact prior behavior. top_tier_adaptive opts in:
    `structure_min_pivot_gap_bars: 3`, `structure_ltf_timeframe_minutes: 5`,
    and the companion `structure_event_lookback_bars: 8 → 4` (bar-based
    settings now count 5m bars; halved to keep BOS/CHoCH event freshness
    near ~20 min instead of 40).
  - Measured on NVDA 5/27 through the real code path: 92 → 8 structure
    pivots, and the bias resolved from "neutral" (conflicted HH/EQL on
    noise) to a clean "bullish" HH/HL read. Watch top_tier's first 2-3
    sessions — structure feeds entry bias, structure exits, and HTF
    alignment simultaneously.

- **top_tier_adaptive: lowered min_sr_scalp_score 4.0 → 3.0 (sr_scalp was structurally dead).** *2026-05-27 PM*
  - `_score_sr_scalp` has a theoretical max of 5.0 but an empirical
    ceiling of **3.9** across 13.2k observed cycles (the +1.5
    rejection-wick component rarely co-occurs with all three
    neutral/chop components). The preset's `min_sr_scalp_score: 4.0`
    was therefore *unreachable* — the regime qualified 0 times and
    fired 0 entries from its 2026-05-12 introduction through 2026-05-27.
  - Lowered the manifest default (3.5 → 3.0) and the preset (4.0 → 3.0)
    so the regime can actually qualify and its real-world edge can be
    evaluated. Documented the 3.9 ceiling in the README so the
    threshold isn't set above it again.
  - Note: a separate live runtime `config.yaml` (e.g. on the
    user-managed H: deployment) carries its own `min_sr_scalp_score`
    and must be updated independently.
- **top_tier_adaptive: static-analysis cleanup.** *2026-05-27 PM*
  - `_recent_momentum_pct`, `_entry_bar_confirms`, `_pullback_leg_context`
    converted to `@staticmethod` (no instance state used). Call sites
    via `self.` are unaffected.
  - `_peak_giveback_triggered` low-tier guard simplified to a single
    chained comparison `0.0 < low_tier_min_r <= peak_r < min_r`.

- **top_tier_adaptive: tightened main-tier peak-giveback retain fractions.** *2026-05-27 PM*
  - `RiskManager._peak_giveback_floor_r` retain fractions raised from the
    hardcoded 0.50/0.60/0.70 (1-2R / 2-3R / 3R+ tiers) to configurable
    0.65/0.72/0.78 via new `RiskConfig.peak_giveback_retain_1to2r` /
    `_2to3r` / `_3r_plus`. `_peak_giveback_floor_r` changed from a
    `@staticmethod` to an instance method to read the config.
  - Rationale from intra-trade R-path reconstruction (1m bars, 35
    trades 5/12-5/27): winners captured only **44% of their MFE**
    ($433 realized vs $978 of peak favorable excursion), and made NO
    new highs after their interim peak in-sample — so the loose floors
    were donating realized gains back to the market rather than
    protecting runner upside. Worst cases: AVGO captured 30% of a
    2.31R peak, COP 22-28%, META 29%.
  - Modeled effect: +3.7R additional capture across 8 winners (~$465
    at full size), no winners clipped (none recovered post-peak in
    the sample). At a 2R peak the floor now sits at 1.3R (was 1.0R);
    at 2.5R it sits at 1.8R (was 1.5R).
  - Risk acknowledged: tighter floors exit sooner on a retrace, so a
    future trade that dips into the [old-floor, new-floor] band and
    then recovers to a bigger peak would be clipped. The fractions are
    configurable — tune down if runner-clipping shows up in live data.
  - Updated `TestPeakGivebackFloor` assertions for the new fractions.

### Fixed

- **top_tier_adaptive: soft bias penalty no longer second-guesses the explicit side decision.** *2026-05-27 PM*
  - When `require_explicit_side_decision` makes a pick, `_decide_side`
    has already chosen the side from current-action signals (recent
    return, VWAP, EMA, bar direction). The soft `_bias_penalty` then
    ran on that decided side and docked its regime scores when the
    side disagreed with `effective_bias` (the chg_open-derived bias).
    On a reversal setup — stock down on the day but recovering, where
    Fix A correctly picks LONG — the penalty could push the LONG
    score below `min_pullback_score` and skip the exact entry Fix A
    was built to catch. Now the penalty is skipped entirely when an
    explicit side decision was made this cycle; it remains the sole
    bias mechanism when `require_explicit_side_decision: false`.

### Removed

- **top_tier_adaptive: dead-code cleanup after Fix A + B.** *2026-05-27 PM*
  - Removed the hard screener-bias veto block from `entry_signals`
    (and its `screener_bias_counter_to_tradable_sides` skip reason).
    Fix A's explicit side decision uses current-action signals to pick
    side; re-overriding that with the screener's session-time bias
    (which can be minutes stale) re-introduced the backward-looking
    decision Fix A was designed to replace. `respect_screener_bias`
    param remains — still used by the soft-penalty / trailing-bias
    fallback path when `require_explicit_side_decision: false`.
  - Removed the recent-momentum disagreement gate (Fix E from
    earlier today). Fix A's vote #1 IS the recent-return signal (at
    0.1% threshold). Fix E hard-blocked on the same signal (at 0.3%
    threshold). After Fix A narrows `preferred_sides` to one side,
    Fix E was a no-op in every case except an unreachable edge.
    Removed params: `recent_momentum_lookback_bars`,
    `recent_momentum_disagree_threshold_pct`,
    `orb_bypass_recent_momentum`. The `_recent_momentum_pct` helper
    stays — Fix A's `_decide_side` still uses it.
  - Removed the pullback-bounce-confirmation gate from
    `_build_pullback_signal`. Fix B (confirmation-bar) does the same
    check more reliably on the LAST FULLY CLOSED bar (not the
    in-progress bar) and applies it to all direction-following
    regimes uniformly. Removed params:
    `pullback_require_bounce_confirmation`,
    `pullback_bounce_close_position_min`.
  - Net: -3 gates per cycle, same coverage, single source of truth
    for side selection (`_decide_side`) + post-decision filters
    (RS, pullback maturity, stretched cooldown, confirmation bar).

### Added

- **top_tier_adaptive: explicit side decision + confirmation-bar entry (Fix A + B).** *2026-05-27*
  - **Fix A — Explicit side decision before regime scoring.** Replaces
    the implicit "evaluate both sides per regime, pick highest score"
    with an evidence-based vote across CURRENT price-action signals.
    The old approach could pick SHORT just because the SHORT regime
    score was 0.5 higher even when every meaningful current-action
    signal said LONG. New `_decide_side(ltf, close, vwap, ema9, ema20)`
    votes across: (1) recent return over `side_decision_recent_lookback_bars`
    (default `6` = 30 min at 5m), threshold
    `side_decision_recent_threshold_pct` (default `0.1`); (2) close
    vs session VWAP with `side_decision_vwap_buffer_pct` dead-band
    (default `0.0005`); (3) EMA9 vs EMA20; (4) last 3 LTF bars'
    green-count. Side wins when votes ≥ `side_decision_min_votes`
    (default `3`) AND opposing ≤ `side_decision_max_opposing`
    (default `1`). Mixed → skip the candidate. The wrong side is
    never evaluated, so all downstream filters only see the decided
    side. Bypassed during ORB via `orb_bypass_side_decision: true`
    (early-session signals are gap-dominated). Disable with
    `require_explicit_side_decision: false`.
  - **Fix B — Confirmation-bar entry trigger.** Companion to Fix A.
    For direction-following regimes (trend / pullback / momentum /
    vol_squeeze), the LAST FULLY CLOSED LTF bar must confirm direction
    before `_build_<regime>_signal` is called: green AND > prior close
    for LONG (mirror for SHORT). Catches single-bar fakeouts where the
    in-progress bar tipped a score threshold but the actual completed
    bar didn't carry the move. Range and sr_scalp regimes are EXEMPT
    — both are mean-reversion theses where the last closed bar moves
    AGAINST the entry direction by design. New `_entry_bar_confirms`
    helper reads `ltf.iloc[-2]` (last closed) and `ltf.iloc[-3]`
    (prior closed). Bypassed during ORB via
    `orb_bypass_entry_confirmation_bar: true`. Disable with
    `require_entry_confirmation_bar: false`.
  - Modeled 5/27 effect: 3 of 6 trades skipped (NVDA SHORT — votes
    1L/2S mixed, NEM LONG — votes 2L/2S tied, COP LONG — votes 2L/0S
    below min_votes=3). 3 enter (NFLX LONG +$67 winner, CVX SHORT
    −$64 strong consensus, DOW SHORT −$41 strong consensus). Net
    P&L: −$38.14 vs −$135.55 original (−71.9% loss reduction). Trade
    count cut in half. The two unblocked losers had unanimous SHORT
    votes — they lost from post-entry reversals, not pre-entry
    direction errors.

- **top_tier_adaptive: recent-momentum disagreement gate (Fix E).** *2026-05-27*
  - The strategy's entire bias chain (`screener.directional_bias`,
    `live_bias`, `trailing_bias`, soft `_bias_penalty`, relative-strength
    gate) derives from `change_from_open` — a session-wide, backward-
    looking quantity. On stocks that have reversed intraday, day_strength
    still reflects the original direction while recent price action has
    flipped. Forensic of 5/27 NVDA SHORT @ 12:36: chg_open −1.66% (all
    signals SHORT), MSHTF bearish, XLK confirmed SHORT — but NVDA had
    bounced 21% off the 11:00 low; bot shorted into the recovering tape.
  - New `_recent_momentum_pct(ltf, lookback_bars)` helper returns the
    percent change between the LTF frame's current close and the close
    `lookback_bars` bars earlier. Returns `None` when there aren't
    enough bars or the prior close is non-positive.
  - New gate in `entry_signals` after the relative-strength filter:
    when `recent_pct >= recent_momentum_disagree_threshold_pct`, SHORT
    is removed from `preferred_sides`; when `recent_pct <= -threshold`,
    LONG is removed. If `preferred_sides` becomes empty, the candidate
    is skipped with reason `recent_momentum_disagrees_local_(up|down)(...)`.
    Defaults: `recent_momentum_lookback_bars: 6` (= 30 min at
    `ltf_minutes: 5`), `recent_momentum_disagree_threshold_pct: 0.3`
    (above typical 5m bar noise ~0.1-0.2% on mega-caps, catches
    reversals not random ticks). `orb_bypass_recent_momentum: true`
    skips the gate during 09:35-`orb_end_time` (early-session momentum
    is gap-dominated and not informative).
  - Defaults are conservative — dry-run on 5/27 showed no winners
    blocked and no losers blocked either at 30 min / 0.3% (today's
    losers had local momentum AGREEING with the losing side at entry;
    they failed via post-entry reversals, not pre-entry disagreement).
    Tune `recent_momentum_lookback_bars` longer (e.g. 12 = 60 min) to
    catch slower reversals like NVDA's 90-min bounce, at the cost of
    a more aggressive filter that may block legitimate trades on
    other days.

- **top_tier_adaptive: pullback maturity check (Fix C).** *2026-05-27*
  - New `_pullback_leg_context(side, session_ltf, current_close, ltf_minutes)`
    helper on `TopTierAdaptiveStrategy` returns `(minutes_since_extreme,
    retrace_pct)` for the side's session extreme. For LONG: extreme =
    session high, anchor = lowest low at-or-before the high bar,
    `leg_size = high − anchor`, `retrace_pct = (high − current_close) /
    leg_size`. Mirror for SHORT. `minutes_since_extreme` is estimated as
    `bars_since_extreme * ltf_minutes` (LTF bar grid is uniform within
    the session, avoids per-bar timestamp arithmetic). Returns
    `(None, None)` when context can't be computed reliably (single-bar
    session, non-positive leg_size).
  - New `_build_pullback_signal` gate at the top of the build: when
    `pullback_require_fresh_leg` (default `true`), reject when BOTH
    `minutes_since_extreme > pullback_max_minutes_since_session_extreme`
    (default `45.0`) AND `retrace_pct > pullback_max_leg_retrace_pct`
    (default `50.0`). AND-logic on purpose: fresh-but-deep retracements
    and old-but-shallow ones both still trade. Targets the 5/27 NEM
    pattern (LONG at 14:48, session high at 07:45 = 400 min stale,
    retracement 140% off the peak — the entire leg gone) which the
    other entry-quality gates didn't catch.
  - Modeled 5/27 effect: blocks NEM (-$36.75 saved). NFLX (+$67) and
    COP (+$17) winners pass cleanly (25 min / 34% and 45 min / 38%
    respectively — neither condition triggered). Holds NVDA SHORT and
    DOW SHORT as "stale but shallow" (95 min / 21% and 255 min / 26%);
    those need a different signal.

- **top_tier_adaptive: four entry-quality gates + low-tier peak-giveback.** *2026-05-26*
  - **Hard screener-bias veto.** Gated by the existing `respect_screener_bias`,
    skips a candidate entirely when `c.directional_bias` is set and lies
    outside `preferred_sides` (e.g., screener=SHORT with `allow_short:
    false`). The Fix A soft penalty (section 6a) drags counter-bias scores
    down but doesn't outright filter weak-bias setups; on 5/26 the per-cycle
    `bias_pen` ranged 0.14-0.21 and gated nothing, despite the screener
    flagging 3,959 SHORT vs 2,533 LONG candidates that day.
  - **Relative-strength gate.** New manifest knobs
    `relative_strength_block_threshold_pct` (default `0.5`) and
    `orb_bypass_relative_strength` (default `true`). Filters
    `preferred_sides` based on `(candidate.day_strength −
    sector_ETF.day_strength)`. Catches stocks under-performing their
    sector — 5/26 INTC at +0.21% vs XLK +1.27% (rel −1.06%, lost $3),
    INTC at 14:04 at +0.10% vs XLK +0.90% (rel −0.80%, lost $64), NEM at
    −0.19% vs XLB +0.67% (rel −0.86%, lost $10). META (the lone winner)
    had rel +0.25% and passes the gate.
  - **Stretched-cooldown hysteresis.** New `stretched_cooldown_minutes`
    (default `3.0`). The `reject_stretched_entries` thresholds
    (`stretched_percent_b_max`, `stretched_atr_mult_max`) are crisp — a
    single tick across relaxes them while the structural condition is
    still active. AMZN on 5/26 was rejected at 10:11:41 with pct_b=0.851
    then entered 46 s later as the close ticked back across (lost $28).
    The cooldown stamps the failure timestamp and rejects subsequent
    checks within the window. Per-symbol regardless of side.
  - **Pullback bounce confirmation.** New `pullback_require_bounce_confirmation`
    (default `true`) and `pullback_bounce_close_position_min` (default
    `0.5`) in `_build_pullback_signal`. Requires the entry bar's close
    to be in the directional half (close_pos ≥ threshold for LONG; ≤
    1-threshold for SHORT) AND on the favorable side of the prior 5m
    bar's close. Blocks the "pullback that's actually a rollover" pattern
    — FCX on 5/26 entered at 10:12:26 with the in-progress 5m bar
    showing close_pos 0.186 (close in the bottom 19% of the bar's
    range), then never bounced and lost $44.
  - **Low-tier peak-giveback.** Three new `RiskConfig` fields:
    `peak_giveback_low_tier_enabled` (default `True`),
    `peak_giveback_low_tier_min_r` (default `0.7`),
    `peak_giveback_low_tier_giveback_frac` (default `0.7`). Catches the
    0.7-1.0R MFE "purgatory" trades that round-trip to BE / fixed stop
    before the main tier (`peak_giveback_min_r: 1.0`) arms. Uses a
    fixed giveback fraction (not the main tier's peak-size-dependent
    ladder) because at sub-1R peaks the run-vs-noise signal is weaker.
    At 0.9R peak with default 0.7 frac, exits when current_r ≤ 0.27R.
    Skipped when the high-conviction `peak_giveback_min_r_override` is
    active (those trades want the wider main-tier leash).
  - Modeled 5/26 effect: 5 of 7 trades blocked (NEM, INTC×2 by RS gate;
    AMZN by stretched cooldown; FCX#1 by pullback bounce), 2 enter
    (META +$10.40 winner, FCX#2 -$38.87 residual). Net P&L: -$28.47 vs
    -$177.66 original (-84% loss reduction).

### Changed

- **top_tier_adaptive: `bias_penalty_saturate_at` 2.0 → 0.75 (manifest default).** *2026-05-26*
  - The original 2.0 saturation assumed daily moves regularly hit ±2%;
    intraday reality on most days is 0.3-1.0%, where the penalty
    produced was 0.15-0.50 — not enough to filter `min_pullback_score:
    3.5` candidates with raw scores 3.75+. At 0.75, a -1% day applies
    full 1.0 penalty (was 0.5); a -0.5% day applies 0.67 (was 0.25).
  - The `config.top_tier_adaptive.yaml` HIGH_VOL preset still overrides
    to 2.5 (high-vol days produce 2-3% day_strength regularly, where
    the manifest default would saturate too fast).

- **0DTE option strategies: option_chain_cache_seconds 4 → 60.** *2026-05-21*
  - Both 0DTE configs previously overrode the default 6s chain-cache
    TTL down to 4s — almost every entry cycle re-fetched the chain.
    For a 6-hour session running 3 underlyings × ~100-200 entry cycles
    that's 300-600 Schwab `option_chains()` calls.
  - The chain content the strategy reads (strike list, OI, volume
    buckets, deltas) doesn't materially change in 60s. Post-selection
    leg quotes get refreshed independently via `fetch_quotes` (still
    on the 4-6s `quote_cache_seconds` TTL) plus the per-build stability
    check loop, so entry pricing freshness is unchanged.
  - Bumped both 0DTE yamls to 60s, with the rationale documented
    inline. Estimated savings: 60-150 redundant chain fetches per
    session (15-25% reduction in option-chain API load).
  - Other audit findings (per-position force=True quote re-fetches,
    dual quote calls between position_manager + execution) were
    considered but not changed: the force pattern exists for fresh
    stop/target evaluation and stale leg quotes have tail risk, and
    the dual-fetch overlap is bounded to fill events (rare). Worth
    revisiting after a few sessions of observation if API load is
    still the binding constraint.

- **Scaffold generator: plugin-type-aware templates aligned with today's contract.** *2026-05-19*
  - `scripts/scaffold_strategy_plugin.py` had drifted from the current
    plugin contract in four places:
    1. Scaffolded **equity** yamls inherited a dead `options:` block
       from the `config.example.yaml` template — directly undoing the
       same-day cleanup that stripped that block from all 14 existing
       equity yamls.
    2. `--plugin-type=option` produced an equity-style screener (TV
       `Query().where(...rvol >= min_rvol)`) instead of the
       local-synthesis pattern both real 0DTE strategies use.
    3. `--plugin-type=option` produced an equity-style `entry_signals`
       that read `candidate.metadata.relative_volume_10d_calc` (zero
       under local synthesis) and didn't include
       `live_activity_score` / `dashboard_directional_bias` public
       hooks — a new option strategy would render with the stub-33%-red
       dashboard ring until manually wired.
    4. Manifest's `capabilities.dashboard.tradable_symbols_source`
       hardcoded `"params.symbols"` for both plugin types — wrong for
       option strategies (should be `"options.underlyings"`).
  - Refactor: split the templates into stock- and option-specific
    variants. `_stock_strategy_py` / `_option_strategy_py`,
    `_stock_screener_py` / `_option_screener_py`,
    `_stock_manifest_params` / `_option_manifest_params`,
    `_stock_manifest_capabilities` / `_option_manifest_capabilities`,
    `_full_config_yaml(name, plugin_type)` strips the `options:` block
    from stock-type scaffolds and keeps it for option-type.
  - Option-type template produces:
    - Local-synthesis screener (mirrors `zero_dte_etf_options/screener
      .py` — synthesizes candidates from `config.options.underlyings`,
      no TV call).
    - Strategy class with `live_activity_score(frame)` and
      `dashboard_directional_bias(frame)` public hooks stubbed with
      the same fail-open + threshold-based pattern the real 0DTE
      strategies use.
    - Manifest with `tradable_symbols_source: options.underlyings`,
      watchlist `active_sources` declaring all the standard option
      sources (`options.underlyings`,
      `options.confirmation_symbols`, `options.volatility_symbol`,
      plus position-metadata descriptors keyed to the new strategy
      name), and quote sources for volatility + valuation legs.
    - YAML scaffolded with the `options:` block kept (option
      strategies actively need it).
  - Stock-type template unchanged in shape; only the YAML generation
    drops the `options:` block.
  - Fixed a latent encoding bug in `Path.write_text(content)` —
    without `encoding="utf-8"` Windows writes em-dashes as cp1252
    bytes (0x97), then py_compile reads as UTF-8 and chokes. New
    option template uses em-dashes in its docstrings; explicit
    UTF-8 encoding now applied to all write_text + read_text calls.
  - Smoke-test asserts: both scaffolds compile, both manifests are
    valid JSON, both yamls are valid YAML, all 12 contract checks
    pass (options-block presence, manifest source declarations,
    public hook presence, local-synthesis vs TV-query).

- **Plugin abstraction: live-publish hooks promoted to public + duck-typed dispatch.** *2026-05-19*
  - The candidate dashboard-publish resolver (added earlier today) had
    a polymorphism leak: `engine._publish_state` gated the live
    activity-score / directional-bias compute behind an
    `is_option_strategy(self.config.strategy)` type check, then used
    `getattr(self.strategy, '_live_activity_score', None)` against
    underscore-prefixed (private) method names. Two issues:
    1. The type check restricted the feature to option strategies even
       though there's no semantic reason an equity strategy couldn't
       provide live overrides if its screener can't populate real
       values at screen time.
    2. Private (underscore-prefixed) method names are not appropriate
       for plugin extension points — `BaseStrategy`'s other public
       hooks (`signal_priority_key`, `dashboard_level_context_spec`,
       `dashboard_candidate_label`, etc.) all use public names.
  - Clean break:
    - Renamed `_live_activity_score` → `live_activity_score` and
      `_dashboard_directional_bias` → `dashboard_directional_bias` on
      `ZeroDteEtfOptionsStrategy` (long-options strategy inherits).
    - Updated internal call site in `_regime_confirm` and all docstring
      / comment references in strategy.py, screener.py, mobile.js, and
      the two strategy READMEs.
    - Dropped `is_option_strategy` from `engine.py` imports and from
      the publish block. `engine._publish_state` now resolves the
      hooks via pure `getattr(self.strategy, 'method_name', None)` —
      any strategy that defines these methods opts into live publish
      automatically, no plugin-type dispatch needed.
  - Documented the new hooks in `_strategies/README.md` under the
    "extension hooks" bulleted list, describing return contracts,
    fail-open semantics, and the duck-typed dispatch model.
  - Net result: option vs equity asymmetry is now SOURCE-ONLY (where
    the activity_score / directional_bias come from — screener time
    vs publish time) rather than DISPATCH (engine doesn't know or
    care which is which).

### Removed

- **Equity strategy configs: drop dead `options:` block.** *2026-05-19*
  - 14 equity-strategy YAML configs each carried a ~54-line `options:`
    block that no equity strategy reads. Validation
    (`config.py:1308`) only requires `options.underlyings` when
    `is_option_strategy(strategy)` is true; every
    `self.config.options.*` reader in the codebase is gated behind an
    option-strategy check (risk.py:226, engine.py:296,
    execution.py option order paths, position_manager.py option
    quote freshness, entry_gatekeeper.py option-chain validation,
    plus the 0DTE strategy + screeners themselves). So for equity
    strategies the block was pure noise.
  - Cleaned `configs/`:
    - `config.closing_reversal.yaml`, `config.mean_reversion.yaml`,
      `config.opening_range_breakout.yaml`,
      `config.momentum_close.yaml`, `config.pairs_residual.yaml`,
      `config.rth_trend_pullback.yaml`,
      `config.volatility_squeeze_breakout.yaml`,
      `config.peer_confirmed_htf_pivots.yaml`,
      `config.peer_confirmed_key_levels.yaml`,
      `config.peer_confirmed_key_levels_1m.yaml`,
      `config.peer_confirmed_trend_continuation.yaml`,
      `config.microcap_pm_breakout.yaml`,
      `config.microcap_gap_orb.yaml`,
      `config.top_tier_adaptive.yaml`.
    - Net `783 lines` of dead config removed (most blocks were 54
      lines; `microcap_gap_orb` had 79 because it carried all the
      newer optional features like `options_breakeven_*` and
      `delta_time_shift_*`; `top_tier_adaptive` had 57).
  - Kept intentionally: `configs/config.yaml` (runtime template — you
    might switch the active strategy at runtime) and
    `configs/config.example.yaml` (documentation example demonstrating
    every block).
  - Loader tolerance verified: `config.py:1270` uses
    `raw.get("options", {})` default, then `ZeroDteOptionsConfig(**{})`
    constructs from dataclass field defaults. Smoke-loaded each
    cleaned config to confirm `cfg.options.underlyings` resolves to
    the default `['SPY', 'QQQ']` and other fields to their dataclass
    defaults (e.g. `max_vix=22.5`). The 0DTE configs continue to
    surface their YAML-supplied values (e.g. `max_vix=24.0` for the
    credit-spread parent).

- **0DTE option strategies: dead RVOL pipeline + stub metadata.** *2026-05-19*
  - The legacy RVOL pipeline (`min_candidate_rvol`, `trend_rvol`,
    `credit_min_rvol`, `credit_max_rvol`, plus the derived
    `candidate_rvol`, `candidate_effective_rvol`,
    `candidate_rvol_profile`, `min_candidate_rvol_required`) was
    superseded by `_live_activity_score` on 2026-05-14 but the dead
    code, dead config keys, and dead metadata-stamped fields were
    left in place "for visibility". After the local-synthesis
    screener switch (2026-05-19) the rvol values became stub-only
    (always `1.0`) — the visibility argument no longer held.
  - Clean break removals (no aliases, no fallback shims):
    - `strategy.py`: 4 dead computations (lines 555-557, 583-584) and
      4 dead metadata-dict entries (lines 922-925), plus the unused
      `rvol_profile_for_symbol` import.
    - Both manifests: `min_candidate_rvol`, `trend_rvol`,
      `credit_min_rvol`, `credit_max_rvol` removed from `params`.
    - Both YAML configs: same 4 keys dropped (plus the trailing
      "NO LONGER GATING" inline comments).
    - Both READMEs + project root README: param tables + "RVOL / tape
      filters" sections updated to describe the live-activity-score
      thresholds (`min_activity_for_entry`, `trend_activity_threshold`,
      `credit_activity_min`, `credit_activity_max`) that actually
      drive the gates.
  - 0DTE screeners: dropped the stub `change_from_open: 0.0` and
    `relative_volume_10d_calc: 1.0` from candidate metadata. No
    downstream consumer reads them anymore (strategy uses `u_day_ret`
    and `_live_activity_score(frame)` directly).
  - `_live_activity_score` got an `frame.empty` check for parity
    with `_dashboard_directional_bias` — functionally equivalent
    (len(empty) == 0 < 20 was already safe), but consistent.

### Changed

- **0DTE option configs: audit pass for liquidity, VIX, and HTF gates.** *2026-05-14*
  - Found three mis-tuned areas after systematic param diff between
    `config.zero_dte_etf_long_options.yaml` and the credit-spread parent
    `config.zero_dte_etf_options.yaml`:
  - **VIX caps were inverted**: long-options had `max_vix: 25.0` while
    parent had `22.5`. Long premium suffers MORE in high VIX (expensive
    entry + vega risk on IV crush) so it should have a LOWER cap.
    Swapped to enforce the `long_max <= credit_max` hierarchy:
    - long_options `max_vix: 25.0 → 22.0`
    - credit-spread parent `max_vix: 22.5 → 24.0`
  - **Liquidity gates were too loose** for 0DTE quality. SPY/QQQ ATM
    0DTE typically trades 5k-20k contracts/day with OI 10k-50k+ and
    bid-ask spreads of 1-3%. Old defaults (vol 500, OI 900, spread 7%)
    admitted illiquid OTM strikes with poor fills. Tightened in BOTH
    configs:
    - `min_option_volume: 500 → 1000`
    - `min_open_interest: 900 → 2000`
    - `max_bid_ask_spread_pct: 0.07 (7%) → 0.04 (4%)`
  - **`htf_lookback_days: 60`** on long-options was excessive. At 15-min
    HTF that's ~1500 bars — way more than needed for HTF structure
    detection. Reduced to 15 (matching parent) for ~390 bars.

### Added

- **Session report: regime-call outcome tracker.** *2026-05-21*
  - New `manifest.json -> regime_call_outcomes` block embedded in
    every per-day session archive. Classifies each
    `ambiguous_regime` decision against the 30-min forward price
    move and aggregates by outcome + hour, so a day-over-day
    comparison surfaces drift in regime-scoring quality without
    anyone running ad-hoc post-mortems.
  - Classifier per call:
    - `right`: top was `*_trend` and price moved >= 1 ATR in that
      direction within 30 min.
    - `wrong`: opposite direction had larger excursion.
    - `flat`: neither direction reached 1 ATR (or top was `range`).
    - `unclear`: insufficient forward bars or missing ATR.
  - Output structure: `total_unique_calls`, `by_outcome`
    (right/wrong/flat/unclear counts), `right_pct_of_directional`
    (a quick health metric), and `by_hour` (HH → outcome breakdown).
    Dedupes by `(symbol, minute, top_score)` so a single decision
    repeated in many cycles within one minute is counted once.
  - Reads from the already-written `decisions.csv` + `bars/1m/` in
    the same archive directory. Returns `{}` on any I/O / parse
    failure — never crashes the manifest write.
  - Validation against 2026-05-21 archive: 31 unique calls, 2
    right, 11 wrong, 18 flat (right% = 6.45%). Matches the manual
    post-mortem numbers exactly. Hourly breakdown shows the 10am
    whipsaw window where 10 of the 11 "wrong" calls landed.

- **0DTE credit spreads: pivot-buffer gate.** *2026-05-21*
  - New `OptionsConfig` knobs:
    - `credit_pivot_buffer_gate_enabled` (default `False`)
    - `min_short_strike_pivot_buffer_atr` (default `1.0`)
  - When enabled, rejects credit-spread entries whose short strike is
    within `buffer_atr * atr` of the recent market-structure pivot:
    - bear_call: max(LTF / HTF reference_high) → short must sit >=
      buffer ATR ABOVE
    - bull_put: min(LTF / HTF reference_low) → short must sit >=
      buffer ATR BELOW
  - Diagnosis from 2026-05-21 session post-mortem: both bearish
    credit entries at 11:13 had the short strike essentially AT the
    most recent pivot high (SPY short 741 / mshtf_reference_high
    740.615 → $0.39 cushion = 0.71 ATR; QQQ short 712 / reference_high
    711.89 → $0.11 cushion = 0.15 ATR). Both stopped within 30
    seconds on resistance_break_exit for -$60 combined.
  - The existing `credit_distance_gate_enabled` (1.8 ATR from
    CURRENT spot) passed both setups because price was a few strikes
    away — but missed that the short was sitting ON the pivot. The
    new gate measures from the pivot itself.
  - Verified counterfactual: with gate active today, both BEAR
    entries would have been blocked; the 3 BULL setups (all with
    cushion 4.3-5.0 ATR) would have passed unchanged. Net PnL +$52
    instead of -$8.
  - Enabled in `config.zero_dte_etf_options.yaml` with default
    threshold. Long-options yaml left untouched (long premium
    doesn't have a "short strike" in the same sense).

### Fixed

- **Session archive: trades.csv empty when daily fire runs before bot shutdown.** *2026-05-20*
  - Diagnosis from 2026-05-20 session: bot took 2 SPY credit-spread
    trades (both stopped, -$2 each → -$4 realized). Both appeared in
    `events.jsonl`, `bot_*.log`, and the account snapshot — but the
    archive's `trades.csv` was empty (only header) and
    `manifest.trades_today` read 0.
  - Root cause: `_maybe_export_session_archive` (engine.py:493) fires
    once per ET trading day at ~16:00 ET when the equity session
    ends, calling `_export_session_archive` directly. But the
    cumulative `.logs/trades.csv` is only appended-to by
    `write_session_report`, which **only runs on bot shutdown**
    (engine.py:627). So the daily archive reads from a CSV last
    touched at the previous bot shutdown — empty for today.
  - Fix: `export_session_archive` (session_report.py:834+) now reads
    closed trades **directly from `account.trades`** (filtered to
    today's ET date via `exit_time.astimezone(now_et().tzinfo).date()`)
    instead of filtering the stale cumulative CSV. The `account`
    parameter was already passed in but was being ignored for this
    purpose. Cumulative CSV append on shutdown still happens
    unchanged — daily archive no longer depends on it.

- **0DTE credit spreads: 183 `no_hedge_leg` skips per session due to chain truncation + fractional widths.** *2026-05-20*
  - Two root causes stacking, both surfaced on 2026-05-20 session:
    1. `_fetch_raw_option_chain` requested `strikeCount=12` — for
       SPY at 740, that returns strikes 734-745. Short leg picked by
       0.23-delta lands at ~735 (edge of chain). Hedge target 2.5
       below = 732.5, **outside the 12-strike window**, so
       `choose_nearest_strike("lower", 732.5)` returned None.
    2. `_adaptive_strike_width` rounded scaled widths to nearest
       $0.50 — produced fractional targets (732.5, 701.5, 702.5)
       even when the chain only has integer strikes for these ETFs.
  - Fixes:
    - `strikeCount: 12 → 24` — ±12 around ATM gives credit-spread
      hedges room without meaningfully changing API cost or
      liquidity-filter processing time.
    - `_adaptive_strike_width` now snaps to whole dollars via
      explicit `int(scaled + 0.5)` (ceiling-on-half so 2.5 → 3, not
      banker's-rounded 2) and clamps `>= base_width` so the gate-up
      step never silently collapses to a no-op when scaling is just
      above 1.0.
  - Together: today's chain returned (with strikeCount=24) would
    have included strike 732 — `choose_nearest_strike("lower", 732)`
    finds it cleanly. No fractional targets generated. Should
    eliminate the `no_hedge_leg` skip class entirely.

- **Dashboard focus card: long skip reasons no longer push pills off the right edge.** *2026-05-20*
  - The compact decision-label on the focus-meta line (top-left of
    main focus card) could read e.g. `"skipped: option quote
    unstable"` or `"skipped: insufficient underlying bars"` — long
    enough to crowd the right-side `Last / Change / Spread / Vol`
    pill stack and break the chart-head layout.
  - Two-layer fix:
    - **JS abbreviation map** (`COMPACT_DECISION_REASON_LABELS` in
      `dashboard.js`) — 28 entries collapsing the most verbose
      tokens (`insufficient_underlying_bars → low bars`,
      `option_quote_unstable → quote unstable`,
      `no_contract_near_target_delta → no delta match`, etc.).
      Saves 10-30 chars per token. Unmapped tokens still fall
      through to the existing underscore→space humanizer.
    - **Drop the `"<action>: "` prefix in compact form** —
      `entryDecisionLabelCompact` no longer prepends
      `"skipped: "` / `"error: "`. The trade-not-taken state is
      implied by the muted styling and the absence of an active-
      position chip elsewhere on the card. Full-form
      `entryDecisionLabel` keeps the prefix for roomier surfaces.
    - **CSS safety net** — `.focus-meta` gets
      `white-space: nowrap; overflow: hidden; text-overflow:
      ellipsis; max-width: 100%`. `.chart-head > div:first-child`
      gets `min-width: 0; flex: 0 1 auto; overflow: hidden` so the
      left wrapper can shrink. Any future verbose token that
      bypasses the abbreviation map truncates with `…` instead of
      breaking layout.

- **0DTE strategy: KeyError 'nearest_bullish' when `use_fvg_context` disabled.** *2026-05-19*
  - `_regime_confirm` had a partial fallback dict at strategy.py:707-708:
    `{"bull_score": 0.0, "bear_score": 0.0, "directional_pressure": 0.0}`
    — but the entry-context metadata stamping at lines 952-961 reads
    four more keys unconditionally (`nearest_bullish`, `nearest_bearish`
    on both htf_fvg_score and fvg_ltf_score). When `use_fvg_context`
    was False, accessing `htf_fvg_score["nearest_bullish"]` raised
    `KeyError: 'nearest_bullish'`, which engine.py caught and
    rendered as `"Error: 'nearest_bullish'"` in the status banner.
  - Fix: padded the disabled-fallback dict to mirror the full shape
    that `_score_fvg_context` returns when enabled (adds
    `timeframe_minutes: 0`, `nearest_bullish: {}`, `nearest_bearish:
    {}`). The empty dicts are safe — the downstream
    `.get("state", "none")` and `.get("midpoint")` calls gracefully
    resolve to the disabled-state defaults.
  - Test suite missed the bug because no test exercises the
    `use_fvg_context=False` path; only surfaced when a runtime config
    disabled FVG context.

- **0DTE candidate / watchlist tiles: Day% now resolved at publish time when live quote is missing.** *2026-05-19*
  - The same-day local-synthesis cleanup removed the
    `change_from_open: 0.0` stub from 0DTE candidate metadata
    (rightly — a misleading 0.00% pretending to be a real value). But
    the dashboard fallback chain
    (`q.percent_change ?? row.change ?? row.change_from_open`)
    now lands on `None` whenever the live Schwab quote's
    `percent_change` is momentarily missing (gap between cycle quote
    refreshes and stream ticks), rendering `"—"` instead of the real
    tape value.
  - Fix mirrors the live_activity_score / dashboard_directional_bias
    pattern: added a third optional public hook
    `dashboard_change_from_open(frame) -> float | None` on
    `ZeroDteEtfOptionsStrategy`. Returns the session day-return as
    PERCENT (e.g. `1.23` for +1.23%, matching the unit produced by
    TradingView's `change_from_open` and the Schwab quote's
    `percent_change` that the dashboard already prefers). Computed
    via the same `_session_open_price` RTH-first / extended-fallback
    helper `_regime_confirm` uses internally.
  - `engine._publish_state` resolves the new hook via the same
    duck-typed `getattr` dispatch as the other two — same single
    `data.get_merged` frame fetch per candidate (cycle cache means no
    extra API call), same `math.isfinite` finite-check guard.
  - Documented in `_strategies/README.md` extension-hooks bullet
    (now lists all three resolvers together with their return
    contracts and unit conventions).

- **Dashboard chart: drop canvas-drawn plot-area grid.** *2026-05-19*
  - The chart paint loop was drawing 5 horizontal + 6 vertical grid
    lines on the canvas at fixed proportions of the plot area. With
    the radial-masked CSS overlay grid on `.chart-wrap::before` (32px-
    spaced, theme-aware) the two grids stacked at different spacings
    and the outermost canvas lines doubled as a fake inner border —
    "extra grid + duplicate border" against the new gradient chart
    background.
  - Removed the canvas grid; CSS now owns the grid + outer border
    exclusively. Canvas draws data, axes, and overlays only. The
    `tintRgb` resolution stayed (still used by the time-axis ticks).
  - `dashboard.js` `paint()` loses 15 lines of strokeStyle/lineWidth
    + two for-loops. No data path or tooltip/hover math changed.

- **Mobile candidate activity-score display: use shared scorePct().** *2026-05-19*
  - Mobile was using `Math.round(clamp(score, 0, 1) * 100)` for the
    score readout. With the new tape-aware live activity score (often
    1.5–3.0+ for option strategies), every value clamped to 1.0 and
    every candidate showed 100. Same log-scaled mapping the desktop
    candidate ring uses is now shared via `helpers.js`.
  - Promoted `scorePct(score)` from `dashboard.js` to `helpers.js` so
    both renderers reference one implementation. `dashboard.js`
    references the shared version (definition removed locally).

- **Engine `_publish_state`: NaN/Side type guards on live candidate scoring.** *2026-05-19*
  - The newly-added live activity_score and directional_bias
    resolvers (see Added below) had two latent crash paths:
    1. `float(live_score_fn(frame))` accepted NaN/+Inf silently; those
       would propagate through `_json_safe` → `null` and break the
       candidate ring render instead of falling back to the stub.
       Added `math.isfinite` guard; non-finite results keep the
       candidate stub (1.0).
    2. `directional_bias_for_row.value` was outside the try/except
       block — a subclass returning a string (`"LONG"`) instead of
       `Side.LONG` would crash the entire publish loop and freeze
       dashboard updates until restart. Added `isinstance(_, Side)`
       guard; non-Side returns keep the candidate's existing bias.

- **0DTE strategy `_regime_confirm`: reuse `u_day_ret` for change_from_open.** *2026-05-19*
  - The local-synthesis screener change added a duplicate day-move
    computation using `_same_day_mask` to derive
    `candidate_day_move`. That implementation didn't distinguish RTH
    vs extended-hours and reimplemented logic that already existed
    eight lines above as `u_day_ret` (using `_session_open_price`
    with proper RTH-first / extended fallback).
  - Collapsed 18 lines to 1: `candidate_day_move = u_day_ret`. Strict
    correctness improvement — the canonical helper handles session
    edge cases the duplicate didn't.

- **0DTE option strategies: bypass TV screener — synthesize candidates locally.** *2026-05-19*
  - Diagnosis: 2026-05-19 session ran the bot on
    ``zero_dte_etf_options`` and took 0 trades. Investigation showed
    the TV screener silently returned 0 rows every cycle (visible as
    ``Candidate cycle ... count=0 symbols=none`` log lines). The
    screener uses ``Query().set_markets("america")`` and ``where(c
    ("name").isin(["SPY", "QQQ"]))`` to pull SPY / QQQ. That pattern
    works for stocks but appears to silently return empty rows for
    ETFs under some conditions (no Schwab errors, no TV exceptions —
    just an empty DataFrame).
  - The 0DTE screener has been unchanged since the initial commit and
    was never noticed broken because the bot has been running
    ``top_tier_adaptive`` for the past sessions. Switching strategies
    today exposed the bug.
  - Fix: replaced both screeners (parent + long_options) with PURE
    LOCAL SYNTHESIS — no TV call. The universe is fixed
    (``options.underlyings: [SPY, QQQ]``) and these ETFs are always
    liquid, so a TV existence-check adds zero value. Bypassing also
    eliminates the silent-failure mode entirely.
  - Downstream is unaffected: ``_regime_confirm`` already uses
    ``bars[underlying]`` (Schwab data feed) for all metrics. Live
    ``change_from_open`` is now computed from today's session bars in
    the strategy (was previously sourced from the candidate metadata
    that the TV screener populated). ``relative_volume_10d_calc`` was
    already deprecated by the live-activity-score replacement (ce2f009).
  - Together with the live-activity-score work, the 0DTE strategy is
    now fully Schwab-driven for decisioning — TV is only consulted
    indirectly via the bars feed (which uses Schwab).

### Added

- **Dashboard candidates card: live activity_score + directional bias for option strategies.** *2026-05-19*
  - With the local-synthesis screener, the 0DTE Candidate objects ship
    with `activity_score=1.0` and `directional_bias=None` stubs because
    the screener has no access to streamed bars to compute live values.
    Every SPY/QQQ/IWM candidate tile rendered with a fixed 33% red
    score ring and a permanent neutral tone — visually identical
    regardless of tape state.
  - `engine._publish_state` now resolves both values from the active
    strategy when it's an option strategy and the strategy exposes
    `_live_activity_score` / `_dashboard_directional_bias`. Single
    `data.get_merged` frame fetch shared between both compute paths
    (cycle cache means no API call).
  - Added `_dashboard_directional_bias(frame)` to the parent
    `ZeroDteEtfOptionsStrategy`. Returns `Side.LONG` / `Side.SHORT`
    when VWAP-distance, EMA9-EMA20 gap, and day-return all align;
    `None` otherwise. Long-options strategy inherits.
  - Equity strategies fall through unchanged — their screeners already
    set real activity_score and directional_bias values.
  - Equivalent display now: tape-aware ring fill (log-scaled via
    `helpers.js::scorePct` so the bounded `0..1` ratio maps a 0.5–3.0+
    multiplier into a readable arc) and LONG/SHORT/neutral tone
    matching the underlying's current lean.

- **0DTE option strategies: live activity score replaces TV cumulative RVOL.** *2026-05-14*
  - TradingView's `relative_volume_10d_calc` is session-cumulative
    (today_so_far / 10d-avg-daily), so SPY/QQQ structurally read low
    all morning and only approach normal RVOL late in the day. That
    made the legacy `trend_rvol: 1.25` threshold effectively
    unreachable for benchmark ETFs during 0DTE entry windows — the
    trend-confirmation bonus was disabled-by-accident for the very
    symbols this strategy targets.
  - Added `_live_activity_score(frame)` to the parent zero_dte_etf_
    options strategy. Computes from streamed bars:
    - 60% volume momentum: `sum(last 5 bars) / (sum(prior 15) / 3)`
      — recent vs prior on a per-5-bars-equivalent basis
    - 40% ATR expansion: `current_atr14 / median(last 20 atr14)`
    - 1.0 = neutral (normal pace for this symbol's last 20 bars).
  - Replaced four use sites in `_regime_confirm`:
    - Hard gate `weak_relative_volume` → `dead_tape` (default
      `min_activity_for_entry: 0.0` = disabled; SPY/QQQ are always
      live so the gate is opt-in)
    - Trend bonus (bull/bear) → `activity_score >=
      trend_activity_threshold` (default 1.15)
    - Credit bonus → `activity_score >= credit_activity_min` (0.80)
    - Credit penalty → `activity_score >= credit_activity_max` (1.40)
  - New OptionsConfig knobs surface in both 0DTE yamls. Strategy-
    specific tuning:
    - `config.zero_dte_etf_long_options.yaml`: `trend_activity_
      threshold: 1.20` (long premium needs more elevation than
      credit spreads)
    - `config.zero_dte_etf_options.yaml`: `trend_activity_threshold:
      1.15`, `credit_activity_min: 0.80`, `credit_activity_max: 1.30`
  - `candidate_rvol` / `candidate_effective_rvol` are still stamped
    in metadata for dashboard / log visibility. They no longer
    influence entry decisions for 0DTE — replaced wholesale by the
    self-normalizing activity score.

- **0DTE option strategies: IV-rank gate + adaptive credit-spread width.** *2026-05-14*
  - **IV-rank gate** — normalizes current VIX against a user-provided
    52-week range rather than using absolute VIX level. Useful because
    VIX 20 means different things in a 12-18 regime vs a 25-35 regime.
    New `OptionsConfig` knobs (defaults disable the gate):
    - `vix_52w_low: 12.0` / `vix_52w_high: 30.0` — the 52-week range
      (refresh quarterly when VIX environment shifts; no live fetching)
    - `min_iv_rank: 0.0` (disabled) — entries blocked when rank below
    - `max_iv_rank: 1.0` (disabled) — entries blocked when rank above
    - Computation: `iv_rank = clamp((vix_last − vix_52w_low) /
      (vix_52w_high − vix_52w_low), 0, 1)`
    - Skip reasons: `iv_rank_too_low`, `iv_rank_too_high`
  - Strategy-specific defaults:
    - `config.zero_dte_etf_long_options.yaml`: `max_iv_rank: 0.75`
      (don't buy expensive premium when VIX is in top 25% of range)
    - `config.zero_dte_etf_options.yaml`: `min_iv_rank: 0.30`
      (don't sell skinny premium when VIX is in bottom 30% of range)
  - Enabled **adaptive_width_enabled: true** in the credit-spread
    parent yaml. `_adaptive_strike_width` scales vertical-spread strike
    widths with `current_atr / 20-bar_median_atr` clamped to `[1.0,
    adaptive_width_max_scale]`. High-vol days → wider strikes (more
    credit, more cushion); quiet days → tighter strikes. Capped at
    1.5× base width (`adaptive_width_max_scale: 1.5`). Long-options
    don't use this — single-leg.

- **`options.min_vix` lower-bound VIX floor for long-premium strategies.** *2026-05-14*
  - New `OptionsConfig.min_vix` (default `0.0` = disabled, preserves
    legacy behaviour). When set > 0 and VIX is below the floor at
    entry-decision time, `_option_entry_block_reason` rejects with
    `vix_below_floor`.
  - Mirrors the existing `max_vix` cap. Together they let you define
    a tradable VIX range: above `max_vix` premium is too expensive
    (vega risk); below `min_vix` daily range is too thin to overcome
    theta + commissions on long premium.
  - Shipped defaults:
    - `config.zero_dte_etf_long_options.yaml`: `min_vix: 12.0`
      (long-premium strategy — needs movement to win)
    - `config.zero_dte_etf_options.yaml`: `min_vix: 0.0` (explicit
      no-op — credit spreads actually want low VIX)
  - Backward compatible: any config without `min_vix` defaults to
    `0.0` and the gate is fully bypassed.

### Fixed

- **zero_dte_etf_long_options: ORB path bug audit fixes.** *2026-05-14*
  - **B1 (real)**: ORB entry path now respects structural and S/R vetoes,
    mirroring the trend-window gate behaviour. Previously, ORB-window
    entries (09:35-10:05) could fire even when LTF structure was bearish
    on a bullish ORB or when SR was broken below the entry price — risky
    for 0DTE long premium (theta bleeds fast on mis-aligned setups).
    Toggleable via `orb_apply_structure_veto` (default `true`) and
    `orb_apply_sr_veto` (default `true`). Set both to `false` to restore
    legacy "fire on any breakout" behaviour.
  - **B2**: Aligned manifest-default and strategy.py-fallback values
    that had silently drifted: `trend_end_time` fallback `"13:25"` →
    `"13:30"` to match manifest; `min_bars` runtime-entry fallback
    `35` → `90` to match `required_history_bars()` init fallback. The
    manifest values were never the issue (they always load), but the
    fallbacks were a silent-drift hazard if manifest loading ever
    failed partially.
  - **B3 + B4**: ORB window endpoints + opening-range window are now
    BOTH configurable, decoupled. New params:
    - `orb_start_time` (default `"09:35"`) — was previously hardcoded
    - `orb_opening_window_start` (default `"09:30"`) — was hardcoded
    - `orb_opening_window_end` (default `"09:34"`) — was hardcoded
    Previously, a user setting `orb_end_time: "11:00"` to extend the
    trading window would still derive or_high/or_low from the
    09:30-09:34 5-min opening — stale references the breakout was
    measured against. Now the user can extend BOTH together.
  - **C1**: Defensive column access — `last["close"]` → `last.get("close")`
    in the per-candidate setup. If a frame somehow ships without a
    standard indicator column (rare warmup / data-gap edge case), the
    `_safe_float` default kicks in instead of a `KeyError`.
  - **C2**: New `orb_opening_min_bars` (default `3`). The opening
    range now requires ≥ N bars in the configured opening window
    before deriving `or_high` / `or_low`. Previously a single
    09:34 bar would have been treated as the "opening range" and
    produced trivially-passable breakout checks (`last_close > 0.0 *
    1.0008` for empty references).
  - Manifest + yaml preset + README updated with all new params and
    explanatory comments.

- **volatility_squeeze_breakout screener: drop per-minute liquidity filters to stop candidate churn.** *2026-05-14*
  - Setting `min_value_traded_1m: 0.0` and `min_volume_1m: 0` in
    `configs/config.volatility_squeeze_breakout.yaml` disables the
    `Value.Traded|1` and `volume|1` TradingView filters (gated on
    `> 0` in `screener_client._liquid_equity_conditions`).
  - Symptom: per-minute volume metrics flicker around any non-zero
    threshold each cycle (a stock trading 9k shares one minute and
    7k the next would oscillate around an 8k floor), causing symbols
    to drop in/out of the candidate list every refresh. That thrashes
    the watchlist/warmup state and produces unstable screener output.
  - Replaced by relying on the session-level `min_volume: 750,000`
    floor (stable across the day) plus the strategy's own per-bar
    `min_breakout_volume_ratio: 1.12 × box-median` check at entry
    decision time.
  - Other strategies (top_tier_adaptive, pairs_residual, opening_
    range_breakout, etc.) retain their per-minute filters — those
    strategies may have valid reasons (e.g. opening-range setups
    need pre-market activity confirmation). Scoped change to
    vol_squeeze_breakout only.

- **volatility_squeeze_breakout screener: resolved squeeze-paradox liquidity filters.** *2026-05-14*
  - After fixing the math-conflicting session_range cap (see entry below),
    the screener was still returning zero symbols. Root cause: the per-
    minute liquidity gates compounded with `min_rvol: 1.35` to filter
    OUT the very setups the strategy is built to find. A SQUEEZE is
    defined by volume CONTRACTING (RVOL drops below 1.0 pre-breakout),
    so requiring elevated minute-by-minute activity at screen time was
    paradoxical. The strategy already has its own breakout-bar volume
    check (`min_breakout_volume_ratio: 1.12 × box-median` in
    `_score_vol_squeeze`) — that's the right place for the "elevated
    volume" gate.
  - Relaxed liquidity and RVOL gates in
    `configs/config.volatility_squeeze_breakout.yaml`:
    - `min_volume: 1,800,000 → 750,000` (still liquid for entry/exit)
    - `min_value_traded_1m: 350,000 → 75,000` (~5x looser)
    - `min_volume_1m: 45,000 → 8,000` (~5.6x looser; allows compressed-
      tape stocks)
    - `min_rvol: 1.35 → 1.00` (normal-or-better volume; pre-breakout
      squeezes can have RVOL down to 0.6-0.9 so 1.0 is the practical
      floor below which the stock is illiquid)
  - Documented in the yaml that `min_market_cap` / `max_market_cap` are
    inert for this strategy (only `_small_cap_base_conditions` enforces
    them; vol_squeeze uses `_liquid_equity_conditions` which doesn't).
  - Manifest default `min_rvol` updated 1.35 → 1.00 to match.

- **volatility_squeeze_breakout screener: relaxed math-conflicting filters.** *2026-05-14*
  - Initial 2026-05-14 tightening set `screener_max_session_range_pct`
    to 0.018 (1.8%), which was mathematically inconsistent with
    `max_change_from_open: 4.5%`: a stock up 2% from open MUST have
    session_range >= 2% (the price moved at least that much), so the
    1.8% cap effectively dropped the change_from_open band from
    0.45-4.5% to ~0.45-1.5% and the screener returned zero symbols
    in normal market conditions.
  - Revised defaults that preserve the screener's "no excess noise"
    intent without the math conflict:
    - `screener_max_session_range_pct: 0.018 → 0.035` (must EXCEED
      `max_change_from_open` to act as a noise filter, not a hard
      contradiction). A 2.5% mover with session_range 3% is clean
      (kept); same mover with 5% session_range is choppy (rejected).
    - `screener_min_price: 12.0 → 10.0` (mild relaxation; still
      filters the smallest low-float volatility traps).
  - Updated manifest defaults + yaml preset + README guidance with
    the math-conflict note so future tightening attempts don't
    repeat the mistake.

### Added

- **Dashboard watchlist: "IX" chip on index-confirmation ETF cards.** *2026-05-14*
  - New blue "IX" chip on watchlist cards for symbols that are streamed
    purely for directional confirmation (XLK / XLC / XLY / XLE / XLB /
    GDX / COPX / etc) rather than as tradable entry symbols. Sits
    alongside the existing green "TR" (Tradeable) and amber "NS"
    (Non-streamable) chips on `symbol-title-row`.
  - Implementation:
    - New `BaseStrategy.dashboard_index_symbols()` method that returns
      the union of `params.index_symbols` + every ETF referenced under
      `params.sector_index_map`. Subclasses can override.
    - New `DashboardCache.index_symbols()` method that delegates to the
      strategy method with a defensive param-walking fallback (mirrors
      the existing `tradable_symbols()` pattern).
    - `engine.py` adds `"index_symbols": dashboard_cache.index_symbols()`
      to the `data` block of the published payload.
    - `dashboard.js` adds `getDashboardIndexSymbols(data)` helper and
      renders the chip in `renderWatchlist()`. The chip is suppressed
      when the same symbol is ALSO tradable (TR wins).
    - `dashboard.css` adds `.index-chip` rule joined with the existing
      `.tradeable-chip` / `.ns-chip` shared sizing block. Color
      hardcoded to sky-blue (`#6ab7ff`) rather than `var(--accent)` —
      `--accent` is mint on nexus / amber on solstice / violet on
      nebula and would visually collide with the green TR or amber NS
      chips on those themes.
  - Mobile dashboard unchanged: `mobile.js` doesn't render a per-symbol
    watchlist (only a count in the subline), so no chip surface area
    there.
  - Strategies that don't use index confirmation return `[]` from
    `dashboard_index_symbols()`, so no chips render for those configs.

### Changed

- **top_tier_adaptive: materials sector now maps to [XLB, GDX, COPX].** *2026-05-14*
  - The `materials` entry in `sector_index_map` was previously `[XLB]`
    only. XLB is dominated by chemicals (LIN/SHW/APD/ECL ~50% weight),
    so the pure-miner symbols in the default tradable universe — NEM
    (gold miner) and FCX (copper miner) — correlate weakly with XLB
    and would get false `pullback_index_not_confirmed` rejections when
    gold/copper were aligned with the trade but chemicals were flat.
  - Now maps to `[XLB, GDX, COPX]` with OR semantics: a NEM LONG
    confirms when GDX (gold miners) OR XLB OR COPX is bullish on the
    sector confirmation gate. CTVA / DOW (true chemicals) still
    confirm via XLB.
  - `H:\TradingBot\configs\config.top_tier_adaptive.yaml`:
    `index_symbols` updated from `[XLK, XLC, XLY, XLE, XLB]` to
    `[XLK, XLC, XLY, XLE, XLB, GDX, COPX]` so the new ETFs are
    streamed. E: tuned preset's materials map updated for consistency
    (no `index_symbols` change since E:'s tradable universe has no
    materials symbols).
  - Manifest default updated to the multi-ETF mapping.

- **volatility_squeeze_breakout: 3-tier targets + tighter screener.** *2026-05-14*
  - **Three-tier target structure** replaces the prior 2-tier (standard /
    runner) system in `_build_*` of the standalone strategy:
    - Standard `target_rr: 2.05 → 1.95` — every qualifying setup
    - Runner `runner_target_rr: 2.4 → 2.6` — promoted when ANY of: msltf
      BoS in side direction, atr_expansion ≥ `min_atr_expansion_mult +
      0.12`, OR strong-quality breakout (ATR exp ≥ 1.25, vol ratio ≥
      1.5, close_pos ≥ 0.78)
    - **Premium `premium_target_rr: 3.2`** (NEW) — strong-quality AND
      msltf BoS event AND `tech_ctx.bollinger_squeeze` flag
  - New params on the strategy:
    `premium_target_rr` (default 3.2), `tiered_targets_enabled`
    (default true), `tier_atr_expansion_floor` (1.25),
    `tier_volume_ratio_floor` (1.5), `tier_close_position_floor` (0.78).
    Set `tiered_targets_enabled: false` to revert to 2-tier behavior.
  - Signal metadata now stamps `squeeze_tier_label` ("standard" /
    "runner" / "premium") and `squeeze_effective_target_rr` for log
    visibility — post-session analysis can slice trade outcomes by tier.
  - **Screener tightening** (more probable symbols, fewer noise traps):
    - `screener_min_price: 8.0 → 12.0` (now param-tunable, was
      hardcoded). Filters low-float volatility traps where small flows
      move the tape disproportionately.
    - `screener_max_session_range_pct: 0.025 → 0.018`. Stocks already
      showing >1.8% intraday range have used most of the day's energy.
    - `max_change_from_open: 7.5 → 4.5`. Stocks already up 5%+ rarely
      have clean continuation runway out of a squeeze.
    - New `screener_rvol_bonus_*` (threshold 1.8, scale 2.0, cap 5.0):
      RVOL-tier bonus added to `_squeeze_focus_score`. Each unit of
      `_effective_relative_volume` above 1.8 adds 2.0 to the score
      (capped at +5.0). Strong-accumulation names rise to the top of
      the ranked candidate list.
  - Motivation: the strategy was overall restrictive (lots of hard
    filters) but the targets were uniform across breakout quality —
    a marginal setup that barely passed all gates was rewarded the
    same as one with full ATR expansion + BoS + Bollinger squeeze.
    Tier system creates linear reward for breakout quality. Screener
    tightening reduces raw candidate count by ~40-50% while focusing
    on the genuinely compressed, mid-cap-and-up names where squeeze
    breakouts have the highest historical success rate.
  - LONG-side and SHORT-side tier logic mirror each other. SHORT
    strong-quality requires `close_pos <= 1.0 - tier_close_position_
    floor` (close near bar's LOW) instead of upper-bar close.
  - `allow_short: false` in the shipped config preserved per user
    direction — code path unchanged.

- **top_tier_adaptive: vol_squeeze hard gates + setup-quality filters.** *2026-05-14*
  - Built `_build_vol_squeeze_signal` with two complementary gate types
    that keep today's known winners (TSLA +$130, COP +$14, XOM +$33)
    while filtering ~5 of 8 losers identified in the ENTRY_CONTEXT log.
  - **Hard breakout-quality gates** (toggle via `vol_squeeze_hard_
    breakout_gates: true`, default `true`) — convert the prior +0.5
    scoring bonuses into HARD gates. Compression-strong setups (3.5
    base) + a marginal breakout (+1.5 = 5.0) used to pass
    `min_vol_squeeze_score: 4.0` even when the post-breakout bar had
    weak volume / wicky body / barely cleared the box. Now hard-reject:
    - Volume: `bar_volume / box_volume_median >= 1.25` (was scoring
      bonus only at 1.20)
    - Close position: `close_pos >= 0.65` for LONG (mirror for SHORT)
    - Breakout buffer: `last_close >= box_high * (1 + 0.0012)` for LONG
  - **Setup-quality gates** (NEW, separate threshold params):
    - `vol_squeeze_min_sr_bias_alignment` (default `0.20`): rejects
      LONG when `sr_bias_score < -0.20` (HTF SR favors the opposite
      side); mirror for SHORT. Today's losers included AMD 10:06
      (sr_bias −0.75), NFLX 13:35 (−0.60), GOOG 10:08 SHORT (+0.75
      against a SHORT). All 3 winners had `sr_bias_score >= +0.15`.
    - `vol_squeeze_min_pct_b_directional` (default `0.50`): LONG
      requires `tech_bollinger_percent_b >= 0.50` (upper half of BBs
      at the breakout bar); SHORT requires `<= 0.50`. Today's AMD
      10:06 LONG had pct_b 0.31 (lower band), AAPL/GOOG SHORTs had
      0.40/0.44 (mid — not at lower band). All 3 winners had
      pct_b ≥ 0.60.
    - Set either threshold to `0.0` to disable that gate.
  - Skip-reason format surfaces the actual values:
    - `long_vol_squeeze_weak_breakout_buffer(close=X<required=Y)`
    - `vol_squeeze_weak_breakout_volume(ratio=X<1.25)`
    - `long_vol_squeeze_weak_bar_close(pos=X<0.65)`
    - `long_vol_squeeze_sr_against(bias=−0.75<−0.20)`
    - `long_vol_squeeze_pct_b_below_mid(pct_b=0.31<0.50)`
  - Motivation: 2026-05-14 session showed 14 vol_squeeze entries with
    3W/8L (27% wr), +$9.58 net — without TSLA winner −$120 net. The
    earlier attempt at raising scoring bonus thresholds was cosmetic
    because the bonuses only add +0.5; most setups passed `min_score:
    4.0` on compression + breakout alone, never needing the bonuses.
    Hard gates close that loophole, AND the new setup-quality gates
    add data-derived filtering that proved to discriminate winners
    from losers in the session log.
  - Earlier "1.40 vol_ratio + 0.75 close_pos" hard gates were aggressive
    enough to risk blocking the TSLA winner. Softened to 1.25 / 0.65
    in this iteration — winners likely pass both, the SR + pct_b gates
    do the heavy lifting on quality filtering.
  - `disable_vol_squeeze_regime` remains `false` on both E: tuned preset
    and H: running config. Manifest defaults updated to match.

### Added

- **Tight EQH+EQL bias suppression (`structure_min_range_atr_mult`).** *2026-05-14*
  - New `support_resistance` knob `structure_min_range_atr_mult` (default `1.5`).
    When EQH and EQL flags both fire on `analyze_market_structure` AND the
    spread between `reference_high` and `reference_low` is below N×ATR, the
    bias resolver short-circuits to `"neutral"` — preventing the midpoint /
    pivot-bias / recent-event paths from flipping bias on noise within a
    tight consolidation. EQL/HH pivot labels remain on the context so
    range-regime entries (which key on EQ flags for mean-reversion setups)
    still see them.
  - Genuine BoS through `reference_high` / `reference_low` (real breakout
    beyond breakout_buffer) still fires bias bullish/bearish — that check
    runs BEFORE the tight-range short-circuit. CHoCH exits unaffected.
  - Two new fields on `MarketStructureContext` surfaced for log analysis:
    - `structure_range_atr`: spread / ATR (always populated when both
      reference levels present, regardless of tightness flag).
    - `tight_structure_range`: bool flag indicating the guard is active.
  - Both fields auto-surface in `ENTRY_CONTEXT` / `EXIT_CONTEXT` /
    `SKIP_SUMMARY` JSONs via the `msltf_` / `mshtf_` prefix in
    `strategy_base._structure_lists`.
  - Motivation: user observation that "EQL and EQH shouldn't be allowed
    to happen right next to each other — there has to be a gap between
    them or they produce false signals." Specifically: AMD 14:36 LONG
    pullback (2026-05-14) was killed at hold=10.2m via
    `structure_bearish_exit:EQL` on a chop range where bias was
    oscillating noisily. With this guard active and `min_range_atr_mult`
    set to 1.5, the bias resolves to neutral inside the tight range and
    the exit doesn't fire on midpoint-bias noise.
  - Threaded through 3 call sites: `strategy_base._structure_context`,
    `data_feed.build_support_resistance_context` (via SR builder kwarg),
    and `dashboard_cache.analyze_market_structure`. Tests in
    `tests/test_bug_regressions.py::TestTightStructureRangeBias2026_05_14`.
- **top_tier_adaptive: oversized entry bar gate.** *2026-05-14*
  - New params on `top_tier_adaptive` to reject entries when the latest
    LTF 5m bar has range or body far above ATR — catches the "5m close
    lag" chase pattern where the bot waits for a large bar to close and
    enters near its high/low (a $X move already done):
    - `reject_oversized_entry_bar` (default `true`): master switch.
    - `entry_bar_range_max_atr_mult` (default `1.8`): skip when
      `(high - low) / atr14 >= 1.8`.
    - `entry_bar_body_max_atr_mult` (default `1.4`): skip when
      `|close - open| / atr14 >= 1.4`.
    - `orb_bypass_oversized_entry_bar` (default `true`): opening flush
      bars are always huge — bypass during ORB window.
  - Applies to `trend` / `pullback` / `sr_scalp` regimes only. `range`,
    `vol_squeeze`, and `momentum` are exempt because big bars ARE the
    setup for those regimes (range = mean-reversion at extremes;
    squeeze + momentum = expansion-driven).
  - Independent of `reject_stretched_entries` (which keys on Bollinger
    %B + ATR-stretch from EMA20). The stretched gate didn't catch
    AMD-style "big bar but price isn't far from MAs" entries because
    EMAs follow the move; this gate looks at the bar's OWN size.
  - Skip-reason format: `long_oversized_entry_bar(range=X.XX>=R.RR,body=Y.YY>=B.BB)`
    surfaces both metrics so the active condition is identifiable.
  - Implementation in `_finalize_signal` right before the existing
    `reject_stretched_entries` block. Tests in
    `tests/test_bug_regressions.py::TestOversizedEntryBarGate2026_05_14`.
- **Structure-exit pullback grace + BoS confirmation gate.** *2026-05-14*
  - Two new ``support_resistance`` knobs that layer onto the existing
    ``structure_exit_grace_minutes`` / ``structure_exit_min_post_entry_pivots``
    gates that suppress ``structure_bearish_exit`` / ``structure_bullish_exit``
    early in a trade's life:
    - ``structure_exit_grace_minutes_pullback`` (default ``15``): extends
      the grace specifically for the pullback regime (``position.metadata
      .regime == "pullback"``). Pullback by design enters into LTF chop —
      the first EQL/LL pivot 10 minutes in is almost always noise, not
      reversal. Other regimes still use the global grace (10 min).
    - ``structure_exit_require_bos_confirmation`` (default ``true``): the
      bias-flip exit now additionally requires an active BoS event
      (``bos_down`` for long-exit, ``bos_up`` for short-exit). Without
      this, bias flips on a single EQL/HH pivot — a noisy, weak signal.
      With it, the bot waits for actual structural break (price below a
      prior swing low / above a prior swing high). CHoCH exits remain
      unaffected — those are already strong signals.
  - Motivation: AMD 14:36 LONG (pullback regime, 2026-05-14) was killed
    at hold=10.2m via ``structure_bearish_exit:EQL``. The exit barely
    cleared both legacy gates (10min/2-pivot); the LTF formed a single
    EQL pivot, bias flipped bearish, exit fired. Price recovered to
    ~$452 (past R1 $450.10, toward R2 $454.65) shortly after — a
    winnable trade aborted on noise.
  - Per-regime grace is implemented in ``strategy_base.position_exit_signal``
    by branching on ``position.metadata.regime``. BoS confirmation is
    applied to both LONG and SHORT bias-flip paths. Tests added in
    ``tests/test_bug_regressions.py::TestPullbackGraceAndBoSConfirmation2026_05_14``.
- **top_tier_adaptive: per-sector index confirmation map.** *2026-05-14*
  - New ``sector_index_map`` param routes each candidate to a sector-
    specific list of index ETFs for entry confirmation, replacing the
    "OR across all ``index_symbols``" behavior. Prevents e.g. an AAPL
    LONG from being confirmed by XLE just because energy happened to
    be bullish-aligned.
  - Default mapping covers all 11 GICS sectors with the canonical SPDR
    Select Sector ETFs: ``tech: [XLK]``, ``consumer_discretionary: [XLY]``,
    ``communication: [XLC]``, ``financials: [XLF]``, ``healthcare: [XLV]``,
    ``industrials: [XLI]``, ``energy: [XLE]``, ``consumer_staples: [XLP]``,
    ``materials: [XLB]``, ``real_estate: [XLRE]``, ``utilities: [XLU]``.
  - New strategy helper ``_indices_for_symbol(symbol)`` walks
    ``sector_groups`` to find the symbol's sector, then reads
    ``sector_index_map[sector]``. Falls back to the broad
    ``index_symbols`` list when no per-sector mapping exists
    (backward-compat for legacy configs).
  - ``_index_confirms`` and ``_index_neutral`` now take a ``symbol``
    parameter; called per-candidate inside ``entry_signals`` (was
    hoisted to once-per-cycle under the broad SPY/QQQ design).
  - Default ``index_symbols`` updated to the SPDR Select Sector ETFs
    covering the default tradable universe's sectors (XLK / XLC / XLY
    / XLF / XLV / XLP). SPY + QQQ removed — they're no longer in any
    sector's map entry, so streaming them was wasted quote bandwidth.
- **top_tier_adaptive: early-session stop widening (Tier 2a companion).**
  *2026-05-14*
  - New params: ``early_session_stop_widening_enabled`` (default true),
    ``early_session_stop_widening_until`` (default ``"10:30"``),
    ``early_session_stop_widening_mult`` (default 1.3).
  - ``_volatility_widening_factor`` now combines two orthogonal
    triggers: (1) the existing ATR-expansion check (RELATIVE), and
    (2) a time-of-day check (ABSOLUTE) that fires during the
    post-open high-vol window. Final factor = ``max(expansion_factor,
    time_factor)`` capped at ``atr_widening_max_factor`` — the two
    don't compound to avoid over-widening on explosive opens.
  - Motivation: an AMD 10:10 LONG was stopped out at $444.46 (entry
    $445.95, $1.49 risk) on a single 1m wick that reversed to $449+
    five minutes later. Tier 2a's relative-expansion check read
    "normal" because all the post-open bars were noisy together. With
    the 1.3x absolute multiplier, the stop would have been ~$444.02 —
    below the dip — and the trade catches the $3+ recovery.
- **Adaptive ladder: triple-gate suppress decision** *2026-05-14*.
  Target-exit suppression in ``position_manager._adaptive_ladder_management``
  now requires THREE confirmations before holding through the multi-
  bar zone flip (previously a single intra-bar tick at target was
  enough to lock the position for 2+ minutes):
  - **Strength gate** (``_ladder_target_strength_confirmed``): the
    last FULLY CLOSED bar must close at/past target with a strong
    directional body (close in the upper/lower 55% of bar range for
    LONG/SHORT). Filters single-tick wicks that revert.
  - **Index re-alignment gate** (``_ladder_indices_still_aligned``):
    re-checks the trade's entry-time ``confirmation_indices`` (newly
    stamped on signal metadata at entry) and verifies at least one
    sector ETF is STILL aligned with the trade direction. If the
    sector tape has flipped since entry, suppress is denied and the
    target exit fires normally — avoids holding through sector
    reversals.
  - **Rung-not-confirmed gate** (existing): the multi-bar zone flip
    hasn't completed yet.
  - Suppress fires only when target_reached + breakout_strength +
    indices_aligned + (NOT rung_confirmed). Any failure → exit at
    target.

### Changed

- **paper_account: per-trade R/R now uses initial stop/target.**
  *2026-05-14*
  - ``_position_to_dict`` reads ``metadata.initial_stop_price`` and
    ``metadata.initial_target_price`` (stamped at entry by
    ``entry_gatekeeper.py:677-678/1215-1216``, immutable thereafter)
    for ``max_risk`` and ``max_reward`` calculation. Falls back to
    live ``stop_price``/``target_price`` for legacy positions.
  - Was: max_risk used the live ``position.stop_price``, so when
    ``adaptive_breakeven_rr`` ratcheted the stop to entry,
    ``max(0, entry - stop) = 0``, max_risk became 0, and the
    dashboard's R/R rendered as ``—`` for every winning trade past
    breakeven (which is most of them).
  - Same payload now also exposes ``initial_stop_price`` +
    ``initial_target_price`` as first-class fields so the
    dashboard's progress bar can keep a stable range as adaptive
    management ratchets the live stop/target.
- **Dashboard: position progress bar uses initial stop/target.**
  *2026-05-14*
  - ``positionRangeSpec`` in dashboard.js now reads
    ``pos.initial_stop_price`` / ``pos.initial_target_price`` with
    fallback to the live values via ``??``. Bar layout stays stable
    through adaptive ratchets (breakeven trail, final-rung clearing
    target to None) so the "where's my stop?" gap doesn't appear.
- **Dashboard: chart marker labels merge on price collision.**
  *2026-05-14*
  - ``pushMarkerLine`` merges labels when a new marker lands at the
    same price as an existing one (e.g. Stop ratchets to entry →
    "Entry / Stop" / "E·ST" combined label) instead of silently
    dropping the second line as a duplicate. The dropped-Stop case
    made it look like the position had no stop on the chart.
- **Dashboard: trade table column "Strategy" → "Regime".**
  *2026-05-14*
  - ``TradeRecord.regime`` (stamped on exit from
    ``position.metadata.regime``) is now the displayed value, with
    fallback chain ``trade.regime || trade.strategy || '—'`` for
    pre-stamp trades. Identifies which of the 6 regimes (trend /
    pullback / range / vol_squeeze / momentum / sr_scalp) produced
    each closed trade.
- **Dashboard: exposure gauge honesty over 100%.** *2026-05-14*
  - Ring fill stays clamped at 100% (preserves the gauge metaphor)
    but the text readout now uses the UNCLAMPED ratio, so 128%
    exposure on a long+short portfolio reads as ``128%`` instead of
    ``100%``. Ring tone flips to ``warn`` (orange) when ratio > 100%.
    Same fix applied to desktop dashboard.js + mobile.js.
- **Mobile dashboard: align topbar with sibling panels + many polish
  tweaks.** *2026-05-13/14*
  - Topbar padding (16px) + box-shadow (var(--shadow)) match the
    ``.panel`` cards below. Inner-pill layout is 3-col grid with
    inline ``label: value`` chips; status row spans full width with
    chip + mode badge left-aligned. Trimmed top padding to compensate
    for ``brand-title`` line-height whitespace.
  - Subline trimmed: drop redundant ``ready X/Y · loading Z`` (already
    in READY pill), abbreviate ``streaming N symbols`` → ``N streams``.
  - New ``API/min`` pill wired to ``data.api_usage.calls_per_minute_5m``.
  - Added Candidates card + Completed Trades card (mobile-only
    compact list views).
  - Removed inner ``overflow-y: auto`` from ``.positions-scroll`` —
    swipes on position cards now pass through to the page scroll
    instead of being eaten by the inner scroll container.
  - Day-% color coding on candidate rows (green/red); trade-row
    dollar amounts intentionally uncolored per user preference.
- **Mobile dashboard: tooltip theme matches active theme.**
  *2026-05-13*
  - ``.chart-tooltip`` ``background`` and ``box-shadow`` switched
    from hardcoded dark blue to ``var(--panel-bg)`` and
    ``var(--shadow)``. Works correctly across all 6 themes
    (default / dark / light / nexus / solstice / nebula).

### Changed

- **top_tier_adaptive config: high-volatility retune.** *2026-05-13*
  - ``configs/config.top_tier_adaptive.yaml`` retuned for elevated-VIX
    tapes. Manifest (``_strategies/top_tier_adaptive/manifest.json``)
    LEFT UNTOUCHED — manifest preserves the shipped low/mid-vol defaults
    so the baseline isn't lost. The yaml is now the deployed high-vol
    preset.
  - **Theme**: bars/extensions are larger in high vol, so
    absolute-distance filters LOOSEN; chop is worse so score gates +
    sr_scalp distances TIGHTEN; giveback is faster so profit-lock
    engages SOONER and locks MORE; ATR expansion triggers stop-widening
    SOONER and goes FURTHER. Soft-bias and high-conviction thresholds
    re-scaled to the bigger day_strength swings high vol produces.
  - **Score / selectivity gates**:
    * ``min_score_gap``: 1.4 → 1.5 (scores noisier; bigger gap for
      decisive regime selection)
    * ``min_adx14``: 16.0 → 18.0 (ADX naturally higher in high vol;
      demand stronger trend reading)
  - **Buffers (absolute distance — bars are larger)**:
    * ``stop_buffer_atr_mult``: 0.25 → 0.30 (wider base buffer; Tier 2a
      scales this further when ATR expands)
    * ``pullback_ema_touch_atr_mult``: 0.35 → 0.45
    * ``pullback_hold_atr_mult``: 0.40 → 0.50
    * ``max_entry_vwap_extension_atr``: 1.50 → 1.80
    * ``max_entry_ema9_extension_atr``: 1.20 → 1.50
    * ``max_entry_bar_range_atr``: 1.80 → 2.20
  - **Stretched filter (bands widen in high vol)**:
    * ``stretched_percent_b_max``: 0.80 → 0.85
    * ``stretched_atr_mult_max``: 1.1 → 1.3
  - **Broken-level clearance (broken levels noisier)**:
    * ``broken_level_min_clearance_pct``: 0.0025 → 0.0035
    * ``broken_level_min_clearance_atr``: 0.72 → 0.90
  - **Target conservatism (SR targets fail more)**:
    * ``target_max_sr_ratio``: 0.8 → 0.7 (30% head-room vs 20%)
  - **Adaptive profit protection (giveback faster)**:
    * ``adaptive_profit_lock_rr``: 1.30 → 1.20 (engage sooner)
    * ``adaptive_profit_lock_stop_rr``: 0.35 → 0.45 (lock more)
  - **Vol-squeeze regime (false breakouts more common)**:
    * ``vol_squeeze_breakout_buffer_pct``: 0.0008 → 0.0012
    * ``vol_squeeze_min_breakout_volume_ratio``: 1.12 → 1.20
  - **Momentum regime (1.5% day strength is common in high vol)**:
    * ``momentum_min_day_strength``: 1.5 → 2.0
  - **sr_scalp regime (S/R failures more common; zones need to be
    further apart and closer-to-edge entries only)**:
    * ``min_sr_scalp_score``: 3.5 → 4.0
    * ``sr_scalp_min_distance_pct``: 0.008 → 0.012 (1.2% zone gap floor)
    * ``sr_scalp_min_distance_atr``: 2.5 → 3.0
    * ``sr_scalp_max_distance_from_zone_atr``: 0.5 → 0.4
  - **Bias (intraday swings bigger; raise thresholds to match)**:
    * ``directional_bias_min_day_strength``: 0.20 → 0.30
    * ``bias_penalty_saturate_at``: 2.0 → 2.5
  - **Tier 2a — ATR-aware stop widening (ATR expansion the norm)**:
    * ``atr_widening_threshold``: 1.3 → 1.2 (trigger sooner)
    * ``atr_widening_max_factor``: 1.5 → 1.8 (more headroom)
  - **Tier 3b — high-conviction peak-giveback override (2.0% is common
    in high vol; raise bar; give conviction trades more runway)**:
    * ``peak_giveback_high_conviction_day_strength_pct``: 2.0 → 2.5
    * ``peak_giveback_high_conviction_min_r``: 2.0 → 2.5
  - **Untouched** (deliberately): score floors per regime
    (``min_trend_score``, ``min_pullback_score``, ``min_range_score``,
    ``min_vol_squeeze_score``, ``min_momentum_score``); regime time
    windows; FVG weights; ladder builder; sector concentration cap; all
    runtime/risk block values (``max_positions``, ``risk_per_trade_*``,
    ``cooldown_minutes`` — runtime-level changes deferred so they
    remain explicit user choices not implicit in a strategy preset).
  - 36 strategy tests still pass. Six tests updated to be insulated
    from yaml preset retunes (read ``bias_penalty_base/saturate_at`` and
    ``atr_widening_threshold/max_factor`` from ``strategy.params``
    dynamically, then verify the formula rather than hardcoded numerical
    outputs). Two pre-existing stale tests (
    ``test_midday_window_allows_pullback_and_momentum``,
    ``test_disable_pullback_removes_it_from_all_windows``) updated to
    include ``sr_scalp`` in the midday allowed-regime set — sr_scalp's
    window (orb_end → no_new) legitimately spans midday, the prior
    expectations predated the 2026-05-12 sr_scalp add.

### Added

- **top_tier_adaptive: new `sr_scalp` regime — HTF S/R mean-reversion
  scalp.** *2026-05-12*
  - 6th regime in the auction. Mean-reversion BETWEEN the bot's existing
    HTF support / resistance zones — NO strategy-local level creation.
    All inputs come from the same sources the rest of the bot uses:
    * Level prices: ``sr_ctx.nearest_support`` (HS) and
      ``sr_ctx.nearest_resistance`` (HR), same fields the dashboard
      labels HS/HR and ``_refine_*_sr_levels`` consume.
    * Zone bands: ``zone_atr_mult * atr`` or ``zone_pct * close`` (max),
      defaulting to the bot-wide 0.20*atr / 0.15%*close. Same formula
      as the dashboard's ``key_level_zones``.
    * Stop nudge: ``sr_ctx.level_buffer`` (with ``vol_widening``).
      Same buffer ``_refine_bullish_sr_levels`` and other S/R code
      use to nudge stops past structural levels.
  - **Distance gate**: the INNER gap ``(HR_zone_lower − HS_zone_upper)``
    must clear BOTH floors (max wins):
    ``sr_scalp_min_distance_pct * close`` (default 0.8%) AND
    ``sr_scalp_min_distance_atr * atr`` (default 2.5x). Too-close zones
    get rejected at build time as ``htf_zones_too_close``; the
    build-queue fall-through then tries other regimes on the same /
    opposite side.
  - **Proximity gate**: close must be inside the entry-side zone OR
    within ``sr_scalp_max_distance_from_zone_atr * atr`` (default 0.5x)
    of its inner edge. Mid-range candles don't qualify.
  - **Permissive scoring**: ``_score_sr_scalp`` rewards bar character
    (lower-wick rejection for LONG, upper for SHORT), VWAP/EMA
    neutrality, low ADX. Max score 5.0; ``min_sr_scalp_score`` default
    3.5. The strict HTF zone check runs at build time, not scoring.
  - **Index-confirmation exempt** (same as range — mean-reversion).
  - **Allowed windows**: orb_end → no_new_entries_after. Skipped during
    ORB to avoid morning level-break chop.
  - **Stop**: ``HS_zone_lower − level_buffer`` (LONG) /
    ``HR_zone_upper + level_buffer`` (SHORT).
  - **Target**: ``HR_zone_lower − level_buffer`` (LONG) /
    ``HS_zone_upper + level_buffer`` (SHORT) — exits at the inner edge
    of the opposite zone, matching the bot's structural-exit
    conventions elsewhere.
  - 4 new tests in ``TestSRScalpRegime`` (36 total in
    ``test_top_tier_adaptive_new_regimes.py``).

- **top_tier_adaptive: Tier 2a — volatility-aware stop widening.**
  *2026-05-12*
  - On trend-day regimes (when current ATR has expanded past
    ``atr_widening_threshold`` × its 5-bar average, default 1.3x), all
    ATR-based stop buffers scale up linearly to ``atr_widening_max_factor``
    (default 1.5x) at 2x the threshold.
  - Risk-per-share widens; risk manager downsizes share count so dollar
    risk per trade stays constant. Effect: fewer false stops from
    trend-day noise, more winners captured without raising trade risk.
  - Applies to all five regime builders (trend / pullback / range /
    vol_squeeze / momentum). Each multiplies its ATR-based buffer
    and the ``default_stop_pct`` floor by the per-candidate widening
    factor.
  - New strategy method: ``_volatility_widening_factor(tech_ctx)``.
  - New config params: ``atr_aware_stop_enabled`` (default ``true``),
    ``atr_widening_threshold`` (1.3), ``atr_widening_max_factor`` (1.5).
  - Stamped on signal metadata as ``vol_widening_factor`` (when >1) for
    post-mortem debugging.

- **top_tier_adaptive: Tier 3b — high-conviction peak-giveback
  loosening.** *2026-05-12*
  - When the candidate's live ``day_strength`` magnitude exceeds
    ``peak_giveback_high_conviction_day_strength_pct`` (default 2.0%)
    at entry, the signal is stamped with
    ``metadata["peak_giveback_min_r_override"] = peak_giveback_high_conviction_min_r``
    (default 2.0).
  - ``risk.py:_peak_giveback_triggered`` reads the override from
    ``position.metadata`` and uses it instead of the global default
    (``config.risk.peak_giveback_min_r``, typically 1.0).
  - Effect: a 2R+ runner on a trend day won't get cut by a normal 50%
    retracement — it has runway to recover and extend. Low-conviction
    trades retain the conservative 1.0R threshold.
  - Per-trade stamp (not session-wide), so each candidate gets its own
    conviction assessment at entry time.
  - 6 new tests in ``TestVolatilityWideningFactor`` (32 total in
    ``test_top_tier_adaptive_new_regimes.py``).

### Changed

- **top_tier_adaptive: regime-to-regime fall-through at build time.**
  *2026-05-12*
  - Old behavior: each side selected ONE regime (the top-scoring one
    after primary + fallback selection paths). If that regime's build
    method failed (e.g. trend's ``no_fresh_breakout``, range's
    ``bollinger_squeeze`` rejection), the side failed entirely — other
    qualifying regimes on the same side were silently ignored.
  - New behavior: each side stores an ordered LIST of qualifying
    regimes (those meeting their ``min_*_score`` threshold) in
    post-penalty score-descending order. The build phase iterates this
    list and tries each regime's build in turn. First successful build
    wins; build failures fall through to the next qualifying regime
    on the same side. Across sides, the higher-scored side gets its
    full build_order tried first.
  - Effect: a high-scoring trend regime that misses its breakout gate
    no longer blocks a qualifying pullback or vol_squeeze from firing
    on the same side. Multiple regimes can coexist on a candidate;
    they no longer compete winner-takes-all for the single slot.
  - ``min_score_gap`` config param is now unused — the primary-vs-fallback
    selection paths it gated are collapsed into the unified
    build-order iteration. Param retained for backwards compat with
    existing configs (silently ignored).

- **top_tier_adaptive: Fix A refactored from hard lockout to soft score
  penalty.** *2026-05-12*
  - Old behavior: when the candidate's live bias was set (e.g. SHORT),
    `preferred_sides` was hard-locked to that single side. The strategy
    never scored or evaluated the opposite side, silently ignoring
    legitimate counter-bias setups (e.g. a bullish BOS + breakout on a
    stock with mildly-negative day_strength).
  - New behavior: both sides are always evaluated. When the side
    disagrees with the live bias, each regime score for that side is
    reduced by `bias_penalty_base * min(1.0, |day_strength| /
    bias_penalty_saturate_at)` before the score-gap auction. Weak
    counter-bias setups are filtered (penalty drags them below
    `min_*_score`); strong structural ones still qualify.
  - Two new params: `bias_penalty_base` (default `1.0`) +
    `bias_penalty_saturate_at` (default `2.0%`).
  - Worked example: a stock with `day_strength = -0.5%` (mild SHORT
    bias) has LONG-side regime scores reduced by 0.25. A trend score of
    5.0 → 4.75 (still above `min_trend_score: 3.5`, qualifies). A trend
    score of 4.0 → 3.75 (still qualifies but margin thinner).
  - 2026-04-20 protection preserved: a stock with `day_strength = -2.0%`
    applies the full 1.0 penalty to LONG-side regimes, blocking weak
    LONG bounces. Stocks with `|day_strength| > saturate_at` get the
    full penalty (no further scaling).
  - Trailing-bias memory unchanged — still infers the bias from recent
    cycles when current `live_bias` is None.
  - `entry_decision` log includes `bias_pen=X.XX` in the failure reason
    when the penalty contributed to no-qualifying-regime, so
    post-mortem can distinguish soft-bias filtering from raw-weak
    scores.

- **top_tier_adaptive: `momentum_close` regime renamed to `momentum`
  and widened from afternoon-only to post-ORB through close.**
  *2026-05-12*
  - Old behavior: regime was restricted to the afternoon window
    (`afternoon_start_time` → `no_new_entries_after`) — i.e., a
    ride-the-bell continuation pattern only.
  - New behavior: regime is allowed in primary
    (`orb_end_time` → `midday_start_time`), midday
    (`midday_start_time` → `midday_end_time`), AND afternoon
    (`afternoon_start_time` → `no_new_entries_after`). The
    `momentum_min_day_strength` hard gate (default 1.5%) is what
    filters chop — stocks without enough intraday move score zero,
    so the time window doesn't need to do the filtering.
  - Methods renamed: `_score_momentum_close` → `_score_momentum`,
    `_build_momentum_close_signal` → `_build_momentum_signal`.
  - Params renamed (clean break, no compat shim):
    `min_momentum_close_score` → `min_momentum_score`,
    `momentum_close_breakout_lookback_bars` →
    `momentum_breakout_lookback_bars`,
    `momentum_close_min_day_strength` → `momentum_min_day_strength`,
    `momentum_close_target_rr` → `momentum_target_rr`,
    `disable_momentum_close_regime` → `disable_momentum_regime`.
  - Regime string in code/logs: `"momentum_close"` → `"momentum"`.
  - **Note for H:\\TradingBot users**: the old param names in
    user-managed configs will silently fall through to defaults
    after upgrading. Rename the keys when you sync.
  - Tests in `tests/test_top_tier_adaptive_new_regimes.py` updated
    for the new name + window.
  - The standalone `momentum_close` strategy
    (`_strategies/momentum_close/`) is unchanged — only the
    top_tier integration was renamed.

- **top_tier_adaptive: directional bias is now computed LIVE in the
  strategy.** *2026-05-12*
  - `_compute_live_directional_bias(frame, close)` reads
    `session_open` from the LTF frame and computes
    `day_strength = (close − session_open) / session_open * 100`.
    Returns `Side.LONG` / `Side.SHORT` / `None` based on a configurable
    threshold (`directional_bias_min_day_strength`, default `0.20%`).
  - Replaces the previous Fix A flow that read the screener's
    pre-computed `c.directional_bias`. The screener value was up to
    ~60s stale and (before this change) was derived from `change`
    (prior-close-relative), which mis-tagged gap-fade days — a stock
    that gapped +2% and faded to flat would read LONG by `change`
    but is actually neutral / SHORT-intent intraday.
  - Trailing-bias memory now records the live bias (not the screener
    bias) so the inferred-bias fallback reflects what live day_strength
    has been doing across recent cycles.
  - The screener (`top_tier_adaptive/screener.py`) now queries BOTH
    `change` and `change_from_open` from TradingView. The
    `directional_bias_fn` and `activity_score_fn` use
    `change_from_open` (matching the strategy's intraday semantic) so
    the gatekeeper's per-side cooldown lookup stays aligned with what
    the strategy will actually evaluate. The previous compat alias
    `rows["change_from_open"] = rows["change"]` (a clean-break
    violation flagged in the prior review) is removed.
  - Dashboard candidate "Day %" continues to display the live Schwab
    `quote.percent_change` (prior-close-relative); the screener
    fallback path is rarely hit during RTH live trading.

### Added

- **top_tier_adaptive: two new regimes (vol_squeeze, momentum_close).** *2026-05-12*
  - **`vol_squeeze`**: Bollinger-squeeze breakout regime. Detects an
    N-bar compression box via `vol_squeeze_lookback_bars` (default 12)
    where `bb_width_pct` and box range are both below configurable
    ceilings, then scores breakout magnitude, confirming volume ratio,
    bar-close position within the breakout candle, and VWAP/EMA
    alignment. Allowed in the primary window (`orb_end_time` →
    `midday_start_time`) and the afternoon (`afternoon_start_time` →
    `no_new_entries_after`).
  - **`momentum_close`**: ride-the-bell continuation regime. Computes
    `day_strength` LIVE from session open + current close (not from
    the screener's `change_from_open`), hard-gates on
    `momentum_close_min_day_strength` (default 1.5%), then scores
    tier-based magnitude + N-bar breakout (1m frame) + alignment.
    **Restricted to the afternoon window only** per user spec —
    pre-afternoon momentum is already covered by trend/pullback.
  - Both regimes compete with trend/pullback/range via the same
    score-gap auction. Independent min-score thresholds
    (`min_vol_squeeze_score: 4.0`, `min_momentum_close_score: 4.0`).
    Independent R:R targets (`vol_squeeze_target_rr: 2.05`,
    `momentum_close_target_rr: 2.0`).
  - **Per-regime opt-out knobs** added for all five regimes:
    `disable_trend_regime`, `disable_pullback_regime`,
    `disable_range_regime`, `disable_vol_squeeze_regime`,
    `disable_momentum_close_regime` (all default `false`). Stripping
    any one removes it from every time window.
  - **Whole-window ORB opt-out** added: `disable_orb_window`
    (default `false`) skips the entire 09:35 → `orb_end_time` window.
    Distinct from the existing `orb_bypass_*` flags which loosen
    filters within the ORB window — this one skips it entirely. Useful
    on tapes where the opening 30 minutes are too whippy and the bot
    should start taking entries at `orb_end_time` instead.
  - All time-of-day boundaries are param-driven; no hard-coded times.
    momentum_close gating reads `afternoon_start_time` and
    `no_new_entries_after` from params (defaults `13:00` / `15:00`).
  - 14 new smoke tests in `tests/test_top_tier_adaptive_new_regimes.py`
    cover regime-window allowance, all five regime disable flags, the
    ORB-window disable flag, and score method robustness on minimal-bar
    frames.

- **LTF/HTF separation cleanup.** The previous code conflated LTF
  (lower-timeframe / trigger frame) and HTF (higher-timeframe / SR
  context) via a silent override pattern: `support_resistance.timeframe_minutes`
  was treated as LTF in name but routinely used as HTF whenever a
  strategy declared `params.htf_timeframe_minutes`. This made it
  impossible to read a config and know what timeframe each block was
  really driving. Cleanup:
  - **Strategy params renamed** for clarity: `htf_timeframe_minutes`
    → `htf_minutes`, `trigger_timeframe_minutes` → `ltf_minutes`. The
    older names are removed entirely (per project's clean-breaks
    convention) — manifests, yaml configs, READMEs all migrated. 35
    files updated.
  - **`support_resistance.timeframe_minutes` is now the default HTF**
    used by SR detection, key-level zones, dashboard sidebar S/R list,
    and engine entry/exit gating. Strategies that operate on a
    different HTF override per-strategy via `params.htf_minutes`.
  - **LTF defaults to 1-minute streaming bars** when a strategy doesn't
    declare `params.ltf_minutes`. Strategies with a distinct intraday
    trigger candle (e.g. `peer_confirmed_key_levels` uses 5-min
    triggers) declare it explicitly.
  - **Helper rename**: `_active_sr_*` / `active_sr_*` / `_sr_*`
    accessors → `_active_htf_*` / `active_htf_*` / `_htf_*`. Each is
    explicit about reading HTF; the old names hid that. New parallel
    `_active_ltf_minutes` / `_ltf_minutes` accessors expose the LTF
    timeframe. The override fallback pattern (read params first, fall
    back to support_resistance block) is preserved — only the names
    are now honest.
  - **Dashboard chart "ltf" mode** renders at the strategy's LTF
    instead of hardcoded 1-minute bars. For `peer_confirmed_key_levels`
    that's 5-min bars; for simpler strategies still 1-min. Mode value
    `"1m"` is accepted as a back-compat alias for `"ltf"` for one
    release, then dropped. Frontend `dashboard.js` migrated to the
    canonical `"ltf"` value.
  - **No trading behavior change for stops/exits/risk** — these were
    already getting HTF via the override; now they get HTF explicitly.
    Streaming responsiveness preserved (flip frame is always 1m,
    live price for stop trigger is always 1m, regardless of HTF).
- **Always-on operation.** Bot now runs continuously across days instead of
  exiting at session close. Three new `RuntimeConfig` knobs:
  `idle_sleep_seconds` (default `60.0`, ~95% overnight CPU savings via
  outside-stream-window cadence), `symbol_state_prune_seconds` (default
  `1800.0`, evicts per-symbol state for inactive symbols on the configured
  cadence — `MarketDataStore.prune_inactive_symbols` /
  `DashboardCache.prune_inactive_symbols`), and `session_reconcile_on_resume`
  (default `true`, re-runs startup reconcile on the first cycle of each new
  ET trading day to catch overnight position changes). Daily session archive
  now fires once per ET trading day after the stream window closes (8pm ET)
  in addition to shutdown. Engine main-loop resilience: exponential backoff
  (2× per consecutive `step()` error, capped at 60s) plus log throttling
  replaces the previous tight 2s retry. Session-rollover hook clears
  `entry_gatekeeper.session_skip_counts` on ET trading-date change so daily
  archives reflect that day's tally only.
- **Order blocks** (`order_blocks.py`). Detection at both 1-minute and HTF
  timeframes with two modes (`loose` / `strict`). Eight knobs in
  `SupportResistanceConfig`: `{ltf,htf}_order_blocks_enabled` enable
  flags plus shared `order_block_mode`, `order_block_max_per_side`,
  `order_block_min_atr_mult`, `order_block_min_pct`,
  `order_block_min_thrust_atr_mult` (default `0.75` — break-of-structure
  thrust filter), `order_block_pivot_span`, and
  `order_block_new_high_lookback`. Strength-based ranking (thrust × size ×
  age × validity) when `max_per_side` clips. New `BaseStrategy` methods:
  `_ltf_order_block_context`, `_htf_order_block_context`,
  `_continuation_ob_retest_plan`, and `_apply_continuation_zone_retest_plans`
  (OR-combine FVG + OB plans). Dashboard chart overlays (dashed border, faint
  fill) via per-profile `show_htf_order_blocks` / `show_ltf_order_blocks` flags;
  cross-timeframe protection mirrors FVG behavior. All 18 shipped presets
  expose the eight OB knobs (defaults safe-off); `peer_confirmed_key_levels`
  ships with OB detection disabled since its custom entry pipeline doesn't
  consume OBs. Reuses every `anti_chase_fvg_retest_*` knob for bar-confirmation.
- **Heal-propagation hook** (`data_feed.py fetch_history`). A successful
  1m heal now invalidates `last_htf_refresh` and the cycle-scoped HTF
  cache so the HTF rebuild fires immediately on the healed 1m frame
  instead of waiting until the next bar boundary. Skipped on empty heals
  (REST returned no candles) since the existing HTF derivation is still
  valid.
- **Quote alias caching** (`data_feed.py`). New `_resolved_quote_alias` cache
  resolves index-like symbols (`NYICDX`→`$NYICDX`, `VIX`→`$VIX`) once and
  routes them through batched `fetch_quotes` instead of issuing a per-cycle
  one-off `quote()` call. Cuts ~1 call/cycle per index symbol.
- **Sliding-window API tracker** (`utils.py SchwabdevApiUsageTracker`).
  Replaces lifetime average with deque-backed sliding windows at 1m / 5m /
  15m / 30m granularities. Snapshot exposes `calls_per_minute_{1m,5m,15m,30m}`,
  raw `calls_window_*` counts, and `lifetime_calls_per_minute`. The legacy
  `avg_calls_per_minute` field is removed entirely — dashboard.js consumers
  read `calls_per_minute_5m` directly so the "Schwabdev Calls / Min (5m)"
  chip reflects current activity instead of being poisoned by overnight
  idle hours. Per project's clean-breaks-over-shims convention. Dashboard
  signature filter excludes the 9 transient rate fields under the
  `('api_usage',)` path.
- **Dashboard chart UX**. Touch-input via pointer events (tap shows tooltip,
  drag moves it, tap persists until next gesture); `touch-action: pan-y` on
  `#market-chart`. Small-phone fallback at `≤480px` (single column, 44×44px
  tap targets, table cells wrap). Hardcoded `DASHBOARD_TIMEZONE =
  'America/New_York'` passed to all chart timestamp formatters. Tab
  `visibilitychange` listeners force immediate refresh on tab return.
- `runtime.max_consecutive_quote_failures` (default `5`): per-symbol
  quote-fetch failure threshold. Symbol is silenced from quote refresh after
  the threshold; recovers on bot restart. Set `0` for legacy always-retry.
- `_strategies.insufficient_bars_reason` promoted to public API. Cross-package
  consumers should `from ._strategies import insufficient_bars_reason`
  instead of reaching into `_strategies.helpers` directly.
- `anti_chase_fvg_retest_skip_vwap_ema9_reclaim` strategy param (default
  `false`). Drops the trend-MA half of the FVG `reclaimed` clause for
  microcap squeeze entries on deep retests where VWAP/EMA9 lag well above
  the FVG zone.

### Added

- **Fib retracement chart overlays (38.2% / 50% / 61.8% / 78.6%).**
  Pullback support levels within a bullish impulse range and bounce
  resistance levels within a bearish impulse range, drawn as dashed
  horizontal lines. Companion to the existing fib extension overlays
  (127.2% / 161.8%). New `show_fib_retracements` chart toggle in the
  `DashboardChartConfig` schema (default `false` compact, `true`
  expanded — paired with `show_fib_extensions`). 8 new
  `fib_bullish_382/500/618/786` and `fib_bearish_382/500/618/786`
  fields on `TechnicalLevelsContext`, computed alongside the
  existing extensions in `technical_levels.py` (no extra impulse
  detection — the same `bullish_impulse` / `bearish_impulse`
  segments drive both extension and retracement levels).

### Changed

- **Technical-levels overlays now follow the strategy's LTF.** The
  dashboard's `symbol_snapshot` previously built the technical_levels
  context (fibs, AVWAP, Bollinger, ADX, channels, trendlines, ATR
  context, OBV, RSI, divergences) from the 1m streamed frame
  unconditionally. With LTF/HTF separation done across the rest of
  the codebase, this was the last pinned-1m surface for derived
  overlays. Now uses the strategy's `params.ltf_minutes` frame
  (resampled via `data.get_merged(symbol, timeframe=f"{ltf_min}min")`
  when LTF != 1, otherwise the 1m streamed frame). For
  `peer_confirmed_key_levels` (LTF=5m) the chart's fib extensions /
  retracements / AVWAP / Bollinger / channels / trendlines all align
  with the 5m bars displayed in LTF chart mode. For default-LTF
  strategies behavior is unchanged.
- **`hourly_*` → `htf_*` rename (HTF concept, no shims).** All
  `hourly_*` strategy params, output keys, methods, and reason codes
  refer to the HTF context (HTF EMAs, HTF zone votes, HTF bias
  alignment) — not literally "the 1-hour timeframe". Renamed for
  consistency with the rest of the codebase's HTF/LTF naming:
  - **Strategy params (2)**: `require_hourly_bias_alignment` →
    `require_htf_bias_alignment`, `strong_setup_min_hourly_vote_edge` →
    `strong_setup_min_htf_vote_edge`.
  - **Method**: `_hourly_bias` → `_htf_bias`.
  - **Output keys (5)**: `hourly_bias` → `htf_bias`,
    `hourly_bull_votes` → `htf_bull_votes`, `hourly_bear_votes` →
    `htf_bear_votes`, `hourly_vote_edge` → `htf_vote_edge`,
    `hourly_vote_bonus` → `htf_vote_bonus`.
  - **Reason codes (5)**: `hourly_bias_bearish` → `htf_bias_bearish`,
    `hourly_bias_not_bullish` → `htf_bias_not_bullish`,
    `hourly_bias_bullish` → `htf_bias_bullish`,
    `hourly_bias_not_bearish` → `htf_bias_not_bearish`,
    `price_not_in_hourly_zone` → `price_not_in_htf_zone`.
  - Touched: 2 manifests (peer_confirmed_key_levels,
    peer_confirmed_key_levels_1m), 2 yaml configs, 3 strategy.py files
    (peer_confirmed_key_levels, peer_confirmed_trend_continuation,
    entry_gatekeeper), 2 READMEs.
- **Dashboard chart: AVWAP renders on HTF charts.** The expanded HTF
  chart was suppressing `show_anchored_vwap` along with the
  `show_ltf_*` toggles. AVWAP is a price-level overlay (horizontal
  line drawn at the anchored-VWAP price) that's valid regardless of
  chart bar timeframe — no reason to hide it on HTF. Removed
  `show_anchored_vwap` from the HTF suppression list in
  `dashboard.js`.
- **Diagnostics tab: bot uptime added.** The bottom-dock Diagnostics
  panel now shows "Bot Uptime" (formatted as `Nd HH:MM:SS` for runs ≥1
  day, `HH:MM:SS` otherwise) and "Started At" (raw ISO timestamp).
  Both derive from `data.started_at` which the engine has been
  emitting in the snapshot payload all along; the dashboard just
  wasn't surfacing it. New `fmtUptime()` helper in `dashboard.js`.
- **All hardcoded "1-minute" paths now follow the strategy's LTF.** Audit
  found four classes of stale 1m hardcodes after the LTF/HTF split, all
  cleaned in one cut:
  - **Real bugs (8 sites)**:
    - `dashboard_cache.py` chart-payload code passed
      `timeframe_minutes=1` and labelled FVGs/OBs `"1m"` even when the
      strategy's LTF was 5m. Both the FVG path (line 961) and the OB
      path (line 1033) now read `self._active_ltf_minutes()` and label
      payloads `f"{ltf_min}m"`.
    - `BaseStrategy._score_fvg_context` was called with
      `timeframe_minutes=1` from `strategy_base.py` (line 2634) and
      `zero_dte_etf_options/strategy.py` (line 577) — both now pass
      `self._ltf_minutes()`.
    - `dashboard.js` `ltfVisibilityFilter` rejected items whose
      `timeframe` label wasn't `'1m'`. With LTF=5m, every LTF FVG/OB
      had label `"5m"` and got dropped from the chart. Filter now
      compares against `ltfTimeframeLabel` derived from
      `chart.timeframe_minutes`. HTF FVG/OB filters likewise switched
      from `timeframe !== '1m'` to `timeframe !== ltfTimeframeLabel`.
  - **`_structure_context(frame, "1m")` → `_structure_context(frame, "ltf")`**
    in 13 strategy files (entry_gatekeeper, strategy_base, mean_reversion,
    pairs_residual, momentum_close, microcap_pm_breakout, closing_reversal,
    opening_range_breakout, rth_trend_pullback, top_tier_adaptive,
    volatility_squeeze_breakout, zero_dte_etf_options ×2). Default value
    of `_structure_context`'s `timeframe` parameter also flipped from
    `"1m"` to `"ltf"`. Strategies with LTF=1m get identical behavior;
    strategies with LTF≠1 (none today, but future-safe) get LTF-aware
    structure analysis automatically.
  - **Stale internal vars** renamed for consistency: `fvg1_score` →
    `fvg_ltf_score` (strategy_base, zero_dte_etf_options),
    `fvg1_ctx` → `fvg_ltf_ctx`, `ms1_ctx` → `ms_ltf_ctx`, `ms1_weight` →
    `ms_ltf_weight`, `ms1_fields` → `ms_ltf_fields`. Output keys
    `fvg_1m_*` → `fvg_ltf_*` (8 keys). Entry-decision metadata key
    prefix `'ms1m'` → `'msltf'` (used by `_structure_lists(prefix=...)`
    in 3 strategies + entry_gatekeeper). Test fixtures in
    `tests/test_bug_regressions.py` migrated to the new
    `msltf_pivot_count` key.
  - **Stale comments/docstrings** updated: `data_feed.py`
    `get_order_block_context` docstring now describes "LTF OBs" instead
    of "1m OBs"; `engine.py` and `strategy_base.py` example tuples for
    `_observed_contexts` use `("structure", "ltf")` instead of
    `("structure", "1m")`.
  - **Genuinely 1m-specific paths kept** (the literal 1-minute frame is
    correct in these): Schwab API `frequencyType="minute"` /
    `frequency=1` for streaming history; `support_resistance.py:563`
    `now_ts.floor("1min")` for the dual-frame flip cutoff;
    `utils.py:658` `ts.floor("1min")` utility; back-compat aliases for
    legacy `"1m"` chart-mode URL parameter (`dashboard.py`,
    `dashboard_cache.py`, `dashboard.html`); `session_report.py:786`
    already LTF-aware.
- **`trigger_*` → `ltf_*` rename (LTF-frame meaning only).** The
  `trigger_*` prefix was overloaded — sometimes meaning "the entry-trigger
  event" (verb), sometimes meaning "the LTF candle / trigger frame"
  (noun). Renamed only the noun-meaning items, with no shims:
  - **Strategy params (12)**: `trigger_quality_bonus_enabled` →
    `ltf_quality_bonus_enabled`, `trigger_quality_max_bonus` →
    `ltf_quality_max_bonus`, `trigger_reclaim_quality_bonus_cap` →
    `ltf_reclaim_quality_bonus_cap`, `trigger_zone_interaction_bonus_cap` →
    `ltf_zone_interaction_bonus_cap`, `trigger_candle_quality_bonus_cap` →
    `ltf_candle_quality_bonus_cap`, `trigger_volume_quality_bonus_cap` →
    `ltf_volume_quality_bonus_cap`, `trigger_range_expansion_bonus_cap` →
    `ltf_range_expansion_bonus_cap`, `min_trigger_score` → `min_ltf_score`,
    `min_trigger_close_position` → `min_ltf_close_position`,
    `min_trigger_volume_ratio` → `min_ltf_volume_ratio`,
    `min_trigger_bar_volume` → `min_ltf_bar_volume`,
    `strong_setup_min_trigger_score` → `strong_setup_min_ltf_score`.
  - **Entry-decision metadata keys (18)**: `trigger_score` → `ltf_score`,
    `trigger_base_score` → `ltf_base_score`, `trigger_quality_*` →
    `ltf_quality_*`, all `trigger_candle_*` (matches / anchor / score /
    net_score / opposite_score / regime_hint) → `ltf_candle_*`,
    `trigger_score_required` → `ltf_score_required`, `trigger_reasons` →
    `ltf_reasons`, `strong_setup_trigger_score_required` →
    `strong_setup_ltf_score_required`, `selection_trigger_score` →
    `selection_ltf_score`. **Skip-reason codes** also renamed:
    `weak_trigger_score` → `weak_ltf_score`, `trigger_score_below_min` →
    `ltf_score_below_min`, `trigger_bar_volume_below_min` →
    `ltf_bar_volume_below_min`.
  - **`BaseStrategy` methods (5)**: `_trigger_score` → `_ltf_score`,
    `_trigger_quality_bonus` → `_ltf_quality_bonus`, `_trigger_quality_caps`
    → `_ltf_quality_caps`, `_configured_trigger_candle_summary` →
    `_configured_ltf_candle_summary`, `_configured_trigger_candle_match`
    → `_configured_ltf_candle_match`.
  - **Internal vars** in the renamed methods: `trigger_min_score`,
    `trigger_window`, `trigger_sweep_window`, `trigger_preview`,
    `level_selection_trigger_score` → `ltf_*` equivalents.
  - **Kept (verb meaning)**: `adaptive_runner_trigger_rr`,
    `exit_trigger_level`, `_pullback_trigger_signal`,
    `_no_style_trigger_reason`, `trigger_lookback_bars` (rth_trend_pullback
    re-expansion trigger event), `trigger_high` / `trigger_low`,
    `trigger_level=` kwarg, locals `trigger_level` / `trigger_broke` /
    `trigger_kind` / `trigger_ref` / `trigger_slice` / `trigger_lookback`,
    `anti_chase_fvg_retest_trigger_tolerance_pct`. These all really mean
    "the thing that triggers entry" (verb), not the LTF frame.
  - 26 files touched in one cut: 5 manifests, 7 yaml configs, 4 strategy.py
    files (peer_confirmed_*, microcap_pm_breakout), `strategy_base.py`,
    `entry_gatekeeper.py`, 7 strategy READMEs + main README, 2 test files,
    `scripts/scaffold_strategy_plugin.py`. No back-compat aliases — old
    names removed entirely (per project's clean-breaks rule).
- **Unified flip-confirmation gate.** The two-mode design
  (`mode="dashboard"` for snappy 1-bar 1m feedback vs `mode="trading"`
  for the strict 2-bar-1m / 1-bar-5m dual-frame OR gate) collapses to a
  single trading-strict gate now that every consumer of the SR context
  uses the same flip strictness:
  - **Dashboard chart** zone-flip detection (`dashboard_cache.py` key
    level zones) switched from `dashboard_flip_confirmation_1m_bars=1` /
    `5m=0` to the trading values. Chart, sidebar (`sr_row()`), entry
    gatekeeper, position management, and strategy entries (`peer_confirmed_*`,
    `top_tier_adaptive`) now all see the same flip status — no path
    where the chart shows a level as broken before the strategy treats
    it as broken.
  - **Entry gatekeeper** (`entry_gatekeeper.py:414`) switched from
    `mode="dashboard"` to `mode="trading"`. Behaviorally a no-op (the
    gatekeeper only reads `sr_ctx.market_structure`, which is computed
    by `analyze_market_structure()` and doesn't depend on flip values),
    but consolidates the cycle-cache (`_cycle_sr_cache`) so the
    gatekeeper and `position_manager` share a single SR context build
    per `(symbol, tf)` instead of two.
  - **`mode="dashboard"` branch removed** from
    `MarketDataStore.get_support_resistance` — only `"trading"` and
    `"default"` modes remain. The `mode` parameter could be retired
    entirely in a follow-up.
  - **`SupportResistanceConfig.dashboard_flip_confirmation_1m_bars`
    removed entirely** (per project's clean-breaks-over-shims rule).
    The orphaned knob is gone from `config.py`, all 18 yaml configs,
    and the SR-config table in README.md.
- **LTF FVG / OB / structure retargeting.** Five SR-config knobs and one
  strategy param were renamed AND re-targeted from "always 1-minute"
  to "the strategy's LTF":
  - `support_resistance.one_minute_fair_value_gaps_enabled` →
    `ltf_fair_value_gaps_enabled`
  - `support_resistance.one_minute_order_blocks_enabled` →
    `ltf_order_blocks_enabled`
  - `support_resistance.structure_1m_pivot_span` → `structure_ltf_pivot_span`
  - `support_resistance.structure_1m_weight` → `structure_ltf_weight`
  - `dashboard.charting.{compact,expanded}.show_1m_fair_value_gaps` →
    `show_ltf_fair_value_gaps` (companion: `show_1m_order_blocks` →
    `show_ltf_order_blocks`)
  - Strategy param `one_minute_fvg_entry_weight` → `ltf_fvg_entry_weight`
  - **Behavior change**: FVG/OB/structure analysis now runs on the
    strategy's `params.ltf_minutes` frame (defaults to 1m streaming
    bars when not declared). For `peer_confirmed_key_levels` (LTF=5m),
    FVGs and OBs are now detected on 5m bars instead of 1m. For
    `peer_confirmed_key_levels_1m` and other strategies that default
    to 1m LTF, behavior is unchanged.
  - **Method renames**: `BaseStrategy._one_minute_fvg_context` →
    `_ltf_fvg_context`; `_one_minute_order_block_context` →
    `_ltf_order_block_context`.
  - **`MarketDataStore.get_fair_value_gap_context`** previously
    accepted a `timeframe_minutes` label but only ever computed on the
    1-minute merged frame. Now it resamples to the requested timeframe
    before building the FVG context (mirroring `get_order_block_context`).
  - **Structure-context gate**: `_structure_context` matches against
    the strategy's LTF via the new `_is_ltf_token()` helper instead of
    the hardcoded `{"1m","1min","minute","execution"}` set. The
    `structure_ltf_*` overrides now apply when the strategy is computing
    structure on its LTF frame regardless of LTF value.
  - **Dashboard chart payload keys**: `one_minute_fair_value_gaps` →
    `ltf_fair_value_gaps`; `one_minute_order_blocks` → `ltf_order_blocks`.
    Frontend `dashboard.js` migrated to read the new keys.
- **Bar-aligned HTF refresh.** `MarketDataStore.should_refresh_htf_context`
  no longer uses an elapsed-time throttle; it now refreshes on HTF bar
  boundaries with a 10-second settle buffer. New HTF data only arrives
  at HTF bar boundaries — within a single bar window the broker has
  nothing new to give us. For HTF=60m (base_freq=30m), at the 11:00
  boundary both 30m constituents of the just-closed 10:00-11:00 60m bar
  are already complete on the broker side, so a single fetch + resample
  produces the closed bar (no need for two 30m-aligned fetches). API
  reduction per symbol per HTF: 5m → 60% fewer fetches/hr; 15m → 87%
  fewer; 30m → 93% fewer; 60m → 97% fewer; 240m → 99% fewer.
  - **`htf_refresh_seconds` removed entirely.** Strategy params,
    manifests, yamls, READMEs, accessors, and the `refresh_seconds`
    parameter on `data_feed.get_*` / `prefetch_htf_contexts` /
    `should_refresh_*` all gone (per project's clean-breaks
    convention). Failure retries work naturally because
    `last_htf_refresh[key]` is only stamped on successful fetch+merge —
    a failed fetch leaves the bar window "due" so the next tick retries.
  - **Cycle-cache key on `get_support_resistance` simplified** —
    `refresh_seconds` dropped from the cycle key tuple since it no
    longer parameterizes behavior.
  - **`current_structure_overlay` no longer rebuilds a full SR context.**
    Calls `support_resistance.analyze_market_structure(frame, ...)`
    directly to extract the CHOCH/BOS overlay without re-running pivot
    detection, S/R clustering, prior-day/week, FVG checks, broken-level
    reconciliation, or proximity metrics — all the work that the
    overlay path threw away. Eliminates a duplicate per-render rebuild
    on every dashboard chart payload.
- **Engine cycle parallelization.** Per-symbol Schwab fetches (history, S/R
  refresh, quote fallback) now run via `_parallel_symbol_map` and
  `_parallel_quote_fetch`. Strategy context caches (`_chart_context`,
  `_structure_context`, `_technical_context`) pre-warm in parallel via the
  new `prime_cycle_contexts(frame, observed)` hook;
  `BaseStrategy._observed_contexts` lazily records context shapes on first
  invocation. Three `RLock`s guard the per-context caches.
  `cycle_precompute_workers` runtime knob controls the thread pool size.
  Per-cycle API rate is unchanged — only burst pattern compressed. 0DTE
  strategies parallel-prefetch option chains (up to 4 workers, scaled to
  miss count) via the new `_fetch_raw_option_chain` helper that splits I/O
  + cache from put/call + liquidity filtering. `startup_reconciler.reconcile`
  parallelizes `account_details` + `account_orders` (~200-400ms boot stall
  saved).
- **Cycle-scoped broker positions cache.** `entry_gatekeeper` fetches
  `account_details` at most once per `engine.step()` regardless of how many
  `broker_position_row` / `broker_position_rows` consumers run inside the
  cycle. New `force_refresh=True` keyword bypasses the cache; backed by a
  `_fetch_broker_positions_uncached()` helper that's the single source of
  truth for the underlying call shape. Failure latches per-cycle to avoid
  retry storms during Schwab outages. New `begin_cycle()` / `end_cycle()`
  lifecycle hooks mirror per-cycle FVG/OB/S-R caches in `data_feed.py`.
  Order block context is also cycle-cached on `MarketDataStore` via
  `_cycle_ob_cache` and `get_order_block_context()`, eliminating ~260
  redundant `build_order_block_context` calls per minute when strategy +
  dashboard both consume OBs.
- **Dashboard HTTPS perf.** HTTP/1.1 keep-alive (`protocol_version =
  "HTTP/1.1"`) with 30s idle timeout — one TCP+TLS connection per browser
  tab instead of fresh pair per request, eliminating first-load stall under
  HTTPS. TLS handshake offloaded to per-request worker thread
  (`do_handshake_on_connect=False` + 5s handshake timeout) so concurrent
  clients handshake in parallel. New `ReusableThreadingHTTPServer.handle_error`
  silences common transport-layer exceptions (`ssl.SSLError`,
  `ConnectionError`, `BrokenPipeError`, `socket.timeout`) at DEBUG level
  instead of dumping full tracebacks to journald.
- **FVG knob consolidation.** Six `htf_*` / `ltf_*` FVG knobs
  collapsed into three shared knobs (`fair_value_gap_max_per_side`,
  `fair_value_gap_min_atr_mult`, `fair_value_gap_min_pct`); both timeframes
  read the same fields. Enable flags
  (`htf_fair_value_gaps_enabled`, `ltf_fair_value_gaps_enabled`)
  remain timeframe-specific. Per project's clean-breaks-over-shims
  convention, the old field names are removed entirely from
  `SupportResistanceConfig`. Mirrors the OB knob consolidation.
- **Helpers.py extraction.** Pure stateless helpers (numeric coercion,
  bar/DataFrame shape utilities, premium clamping, symbol normalization,
  reason formatters, structured-logging payload builder, dashboard
  zone-width policy) extracted from `BaseStrategy` into `_strategies/helpers.py`
  (~622 LOC, 29 functions in 7 sections). `BaseStrategy` shrunk by ~350 LOC.
  ~570 callsites updated across 16 files to import via `..shared`. Class-level
  delegation methods removed entirely. Test patches must switch from
  `patch.object(strategy, "_method", …)` to
  `patch("intraday_tv_schwab_bot._strategies.strategy_base._method", …)`
  (patch the module function, not the class attribute).
- **`peer_confirmed_key_levels` retune.** Always-on profile:
  `auto_exit_after_session: false`, `startup_reconcile_mode: restore_hybrid`,
  entry/management/screener windows expanded to `07:00-19:55` ET,
  `time_stop_minutes: 0`. API-cost retune for extended hours:
  `history_poll_seconds: 300`, `stream_stale_fallback_seconds: 180`.
  HTF refresh is bar-aligned (one Schwab call per HTF bar boundary), so
  the prior `htf_refresh_seconds` knob is gone — see the bar-aligned
  HTF refresh note in **Changed**. Strategy quality filters (min trigger
  score, peer agreement, macro net bias) gate extended-hours candidates
  organically.
- **Dashboard render polish.** `dashboard_recent_trade_markers()` and
  `dashboard_symbol_trade_signature()` filter trades by symbol BEFORE
  slicing (a fresh fill on a long-quiet symbol could otherwise be
  invisible); marker function also filters to today's ET session date.
  Chart payload `last_update` re-stamps on every cache hit so frontend
  timestamp doesn't freeze. iOS `:hover` rules wrapped in `@media (hover:
  hover)` so taps don't stick. Mobile poll cadence floor raised to 4000ms
  (cellular radio savings); honors server-provided `dashboard.refresh_ms`
  when slower than the floor. Order block ranking is strength-based
  (thrust × size × age × validity) when `max_per_side` clips —
  `nearest_bullish_ob` / `nearest_bearish_ob` accessors still resolve
  nearest-by-price for retest-plan consumers. `OrderBlock` enrichment uses
  `dataclasses.replace()` on the slotted dataclass.
- **Plugin scaffold + package hygiene.** `scripts/scaffold_strategy_plugin.py`
  emits 9 FVG knobs + `force_flatten` in the generated manifest, plus SPDX
  headers in scaffolded `__init__.py` / `strategy.py` / `screener.py`. Plugin
  templates updated to call `insufficient_bars_reason(...)` and
  `_safe_float(...)` as free functions (post-helpers.py extraction).
  `top_tier_adaptive` imports `now_et` / `parse_hhmm` from `..shared`.
  `intraday_tv_schwab_bot.__all__` trimmed from 29 unreachable entries to
  `["__version__"]`. `config.__all__` switched to mechanical
  "no-leading-underscore = public" rule (20 entries). `.gitattributes`
  upgraded `* text=auto` → `* text=auto eol=lf` to stop Windows
  phantom-modified states under `core.autocrlf=true`.
- Removed `BaseStrategy._apply_continuation_fvg_retest_plan` (the
  single-plan apply helper that predated OR-combine). All 12 callers across
  4 strategies migrated to `_apply_continuation_zone_retest_plans` with a
  single-element plan list. Removed redundant `dashboard_candidate_levels`
  override in `peer_confirmed_htf_pivots` (returned `[]` matching base
  default). Engine `_cycle_sleep_seconds` collapsed from three branches to
  two (stream-on / stream-off).

### Fixed

- **HTF prior-day/week levels now always candidates, not just fallbacks**
  (`htf_levels.py build_htf_context`). The previous flow only injected
  `prior_day_low` / `prior_week_low` (and the high counterparts) when
  pivot detection produced an empty result. In strong directional moves
  a stock can rally for weeks with no proper pivot lows in the rally
  portion (each bar's low > the surrounding bars' lows by definition of
  an uptrend), so pivot-only support detection surfaces only the ancient
  base. AMD example: rally from $258 → $346 over a week with no pivot
  lows in the rally; the dashboard showed first support at $254 (a
  pivot low from the base period weeks earlier) instead of yesterday's
  $340 low. Both prior-day and prior-week levels now merge into the
  candidate pool alongside pivot levels via `_extend_unique_levels`,
  then compete in `_collapse_same_side_levels`. Their `source_priority`
  of 2.0 (prior_day) / 3.0 (prior_week) outranks pivot's 1.0 in
  `_level_preference`, so when a prior-day/week level overlaps a
  same-cluster pivot, the prior-day/week level wins the picker. The
  second-chance fallback (when filtered candidates are empty) and
  frame-extreme fallback (when both pivots and prior-day/week are
  empty) are preserved as-is for fully-empty edge cases.
- **HTF level scoring now time-aware** (`htf_levels.py _cluster_levels`).
  The previous formula computed `score = touches + min(1.5, 0.15 * touches)`
  — a misleadingly-named "recency_bonus" that was actually a touches
  multiplier with no time component at all. With a 60-day HTF lookback,
  ancient base levels with many touches accumulated during long
  consolidations dominated the top-N selection, evicting recent close-
  to-price swing lows before they reached `_collapse_same_side_levels`.
  AMD example: current price $346.50 with first support showing at
  $257.73 (a 30+-day-old base) instead of the recent $320-$343 swing
  lows. Replaced with the time-aware formula already used in
  `support_resistance.py _cluster_levels`: `recency_factor` decays
  linearly from `1.0` (newest) to `0.10` (oldest) across the cluster
  window, `effective_touches = touches * recency_factor`, plus a
  persistence bonus that rewards levels held across a sustained portion
  of the window. A 30-day-old 8-touch base now contributes ~4 effective
  touches — comparable to a fresh 4-touch swing low — so both survive
  top-N selection and the dashboard renders the full ladder of recent
  + historical levels.
- **HTF in-memory resample reverted.** An earlier attempt at this release
  added a path that rebuilt HTF bars by resampling the in-memory 1m frame
  with a periodic Schwab audit (`htf_audit_refresh_seconds: 3600`).
  Reverted because the convention mismatch between the in-memory path
  (1m bars resampled with `closed="right"` → bars represent ~10:01-11:00
  data) and the Schwab path (30m bars from `price_history` resampled the
  same way → bars represent 10:30-11:30 data) produced inconsistent OHLC
  in the merged HTF frame. Pivot detection on the inconsistent frame
  surfaced wildly stale support/resistance levels (e.g., AMD with current
  price $346 showing first support at $258 from a 30-day-old base). The
  audit knob, `_try_resample_htf_from_live_1m`, `_htf_audit_due`, and
  `last_htf_audit_refresh` tracker are removed entirely. The
  heal-propagation hook on `fetch_history` is preserved (Added section)
  since it's useful regardless of the rebuild path.
- **Strategy correctness.** `peer_confirmed_key_levels._select_level`
  "touched zone" check replaced with per-bar overlap (window-wide
  `low.min()`/`high.max()` could pass when no individual bar's range
  overlapped the zone — fires during news/fast-spike conditions). OB
  detection walk-back uses `continue` instead of `break` so a small doji at
  idx-1 doesn't abort the search for a real OB at idx-2+;
  `_merge_order_blocks` first/last_seen now uses explicit `_earlier`/`_later`
  ISO-timestamp helpers (sort is by price, not chronology). `_optional_int`
  parses float-strings like `"3.7"` → `3` (was returning default).
- **Dashboard chart.** `paint()` uses `activeIndex` consistently (latent
  crash on bar-pinning land — `bars[hoverIndex]` was a stale global).
  `renderEmpty` cancels pending hover-RAF before detaching pointer handlers
  (previously a queued `requestAnimationFrame` from a previous chart could
  draw ghost data after canvas clear). Tap-and-release tooltip persists
  until next gesture (was clearing on finger lift). Volume bars use
  `parseFinite()` (was `Number()` which coerced `"NaN"` strings to `NaN`).
  Theme `<link>` `onerror="this.remove()"` falls back to base styling on
  404. Spread pill no longer flickers visible/hidden between stream ticks
  (now matches sibling pills with `—` placeholder).
- **Mobile dashboard.** `.position-card` cursor override (was `pointer`
  from desktop with no click handler). `.panel-meta` text-wrap fix for
  7-figure equity. `.positions-panel` explicit `position: relative` (no
  longer dependent on desktop's `≤1400px` breakpoint). Qty rendering uses
  `fmtInteger()` (was `escapeHtml()` producing literal `"null"`).
- **Memory + state hygiene.** `data_feed.prune_inactive_symbols` evicts
  the `last_htf_refresh` tuple-keyed dict alongside the rest of the
  per-symbol state.
- **Config + manifest hygiene.** All 18 prod presets + `config.example.yaml`
  expose the eight OB knobs (defaults safe-off) and the three always-on
  knobs (`idle_sleep_seconds`, `symbol_state_prune_seconds`,
  `session_reconcile_on_resume`). README runtime table + behavior
  section updated with the new knobs.
- **Logging + cosmetic.** `dashboard_cache.py log_component_failure` calls
  in OB blocks pass symbol as arg (was printf message). ORB `none`-mode
  activity score rescaled to `rvol × volume / 1_000_000` so log magnitudes
  match other branches. Dashboard `focus-meta` uses compact entry-decision
  label so long ETF skip reasons don't push live-data chips off the card.
  Three IDE / type-checker warnings cleaned up (redundant
  `self.stream = None`, two unused `_ltf_order_block_context` params).

## [1.0.0] — 2026-04-24

Initial public release.

### Infrastructure

- `requirements.txt` strictly pinned to verified versions.
- `pyproject.toml` with setuptools build backend and dynamic version
  from `version.txt`.
- Tests maintained privately in the source tree; not shipped with this
  repository.

[Unreleased]: https://github.com/OWNER/REPO/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/REPO/releases/tag/v1.0.0
