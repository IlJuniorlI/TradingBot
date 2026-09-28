# SPDX-License-Identifier: MIT
"""The 0DTE strategies' option-chain and quote plumbing.

``OptionChainMixin`` holds the per-symbol chain cache, the Schwab chain read,
the liquidity filter and the parallel prefetch. It also holds the vertical
and single-option market validators, with the reason each one refused, and
the quote-stability loop the builders run before they price their legs. The
strategy's ``__init__`` creates the state these methods read
(``_option_chain_cache``, ``_option_chain_read_failed_at``,
``_option_chain_prefetch_failures``) and ``optcfg``.
The mixin moved out of ``strategy.py`` on 2026-09-27; the builders, the
entry gate ``_underlying_below_min_price`` and the entry loop stay there.
"""
import logging
import time as time_mod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime

from ...options_mode import (
    OptionContract,
    contract_from_quote,
    filter_contracts,
    net_price_frac_of_width,
    parse_option_chain,
    single_option_price_bounds,
    vertical_price_bounds,
)
from ...reasons import detail_fields
from ... import sessions
from ...log_setup import ComponentFailureLog
from ...schwab_api import SCHWAB_TRANSPORT_ERRORS, SchwabHTTPError, call_schwab_json

LOG = logging.getLogger(__name__)


class OptionChainMixin:
    """Chain reads and quote checks for ``ZeroDteEtfOptionsStrategy``,
    configured by its ``optcfg`` (``config.options``)."""

    _option_chain_cache: dict[tuple[str, str], tuple[datetime, list[OptionContract]]]
    # Symbol -> when its last option_chains read failed (see __init__).
    _option_chain_read_failed_at: dict[str, datetime]
    # Logs a prefetch read that raised anything else (see __init__).
    _option_chain_prefetch_failures: ComponentFailureLog

    @staticmethod
    def _option_chain_failure_log() -> ComponentFailureLog:
        """The prefetch's failure log, under this module's logger: a
        WARNING with the traceback at most once a minute per symbol, DEBUG
        in between."""
        return ComponentFailureLog(LOG)

    @staticmethod
    def _option_chain_cache_key(symbol: str) -> tuple[str, str]:
        return str(symbol).upper().strip(), sessions.now_et().date().isoformat()

    def _get_cached_option_chain(self, symbol: str) -> list[OptionContract] | None:
        ttl = max(0, int(self.optcfg.option_chain_cache_seconds))
        if ttl <= 0:
            return None
        key = self._option_chain_cache_key(symbol)
        cached = self._option_chain_cache.get(key)
        if cached is None:
            return None
        fetched_at, contracts = cached
        if (sessions.now_et() - fetched_at).total_seconds() > ttl:
            self._option_chain_cache.pop(key, None)
            return None
        return list(contracts)

    def _option_chain_read_failed_recently(self, symbol: str) -> bool:
        ttl = max(0, int(self.optcfg.option_chain_cache_seconds))
        failed_at = self._option_chain_read_failed_at.get(str(symbol).upper().strip())
        if ttl <= 0 or failed_at is None:
            return False
        return (sessions.now_et() - failed_at).total_seconds() <= ttl

    def _set_cached_option_chain(self, symbol: str, contracts: list[OptionContract]) -> None:
        ttl = max(0, int(self.optcfg.option_chain_cache_seconds))
        if ttl <= 0:
            return
        key = self._option_chain_cache_key(symbol)
        if key in self._option_chain_cache:
            self._option_chain_cache.pop(key, None)
        self._option_chain_cache[key] = (sessions.now_et(), list(contracts))
        max_entries = max(1, int(self.optcfg.option_chain_cache_max_entries))
        while len(self._option_chain_cache) > max_entries:
            oldest = next(iter(self._option_chain_cache))
            self._option_chain_cache.pop(oldest, None)

    def _fetch_raw_option_chain(self, client, symbol: str) -> list[OptionContract] | None:
        # Return the full unfiltered 0DTE option chain for `symbol`, or None
        # when Schwab did not return one (see _option_chain_read_failed_at).
        # Reads from the per-symbol cache when warm; on miss, issues one
        # Schwab option_chains call, parses, caches, and returns the
        # result. Pure I/O + cache plumbing — no put/call or
        # liquidity filter applied. Use _fetch_filtered_contracts when
        # you need a filtered list; use this when you only want to warm
        # the cache. An error response is not an empty chain: it is not
        # cached, and the build path reports option_chain_unavailable. A
        # transport failure (SCHWAB_TRANSPORT_ERRORS) and a 2xx body that is
        # not a JSON object (a list, null, a string) read the same; until
        # 2026-09-28 they escaped entry_signals and failed the engine cycle.
        # Anything else the read raises propagates.
        cached = self._get_cached_option_chain(symbol)
        if cached is not None:
            return cached
        if self._option_chain_read_failed_recently(symbol):
            return None
        today = sessions.now_et().date()
        # strikeCount=24 (was 12) — for 0DTE credit spreads the short leg
        # sits at ~0.20-0.30 delta (3-5 strikes OTM) and the hedge then
        # needs another 2-3 strikes further OTM. The previous 12-strike
        # window (±6 around ATM) cut off the hedge target on most credit
        # builds — 2026-05-20 session logged 183 no_hedge_leg skips
        # because the chain returned didn't include strikes far enough
        # from spot. 24 strikes (±12) gives the hedge plenty of headroom
        # without meaningfully changing the API cost or liquidity-filter
        # processing time.
        try:
            payload = call_schwab_json(client, "option_chains",
                symbol=symbol,
                contractType="ALL",
                strikeCount=24,
                includeUnderlyingQuote=True,
                fromDate=today,
                toDate=today,
            )
        except (SchwabHTTPError, *SCHWAB_TRANSPORT_ERRORS) as exc:
            self._record_option_chain_read_failure(symbol, f"{type(exc).__name__}: {exc}")
            return None
        if not isinstance(payload, dict):
            # parse_option_chain reads an object.
            self._record_option_chain_read_failure(
                symbol, f"{type(payload).__name__} body, not a JSON object: {payload!r:.120}")
            return None
        self._option_chain_read_failed_at.pop(str(symbol).upper().strip(), None)
        contracts = parse_option_chain(payload, only_dte=0)
        self._set_cached_option_chain(symbol, contracts)
        return contracts

    def _record_option_chain_read_failure(self, symbol: str, detail: str) -> None:
        """Log a chain read that returned no chain, and remember when, so
        the chain is not re-read for ``option_chain_cache_seconds``."""
        LOG.warning("Option chain read failed for %s: %s", symbol, detail)
        self._option_chain_read_failed_at[str(symbol).upper().strip()] = sessions.now_et()

    def _fetch_filtered_contracts(self, client, symbol: str, put_call: str) -> list[OptionContract] | None:
        """The chain's liquid ``put_call`` contracts; None when the chain
        could not be read."""
        contracts = self._fetch_raw_option_chain(client, symbol)
        if contracts is None:
            return None
        filtered = filter_contracts(
            contracts,
            put_call=put_call,
            min_volume=self.optcfg.min_option_volume,
            min_open_interest=self.optcfg.min_open_interest,
            max_bid_ask_spread_pct=self.optcfg.max_bid_ask_spread_pct,
        )
        return [c for c in filtered if (c.ask - c.bid) <= float(self.optcfg.max_leg_spread_dollars)]

    def _prefetch_option_chains(self, symbols: list[str], client) -> None:
        # Warm the per-symbol option-chain cache in parallel before the
        # sequential candidate-build loop in entry_signals(). Each candidate
        # would otherwise hit Schwab serially via _fetch_filtered_contracts;
        # for N>1 cache-miss candidates that's N * ~150ms of stacked I/O on
        # the engine thread per cycle. The chain cache is symbol+date keyed
        # (no put_call), so one fetch per symbol covers both CALL and PUT
        # build paths. The prefetch is a warm-up and never fails the cycle.
        # A read that found the chain unavailable (an error response, a
        # transport failure, a body that is not a JSON object) is remembered
        # by _fetch_raw_option_chain, so the build path does not re-read it
        # and emits option_chain_unavailable. Anything else a read raises is
        # logged here with its traceback (at WARNING at most once a minute
        # per symbol, at DEBUG in between: the prefetch runs every entry
        # cycle), and the other symbols still warm. It is not remembered: the
        # build path reads that chain again, and its read raises for a
        # candidate a style fires on.
        misses = [
            sym for sym in {str(s or "").upper().strip() for s in symbols}
            if sym and self._get_cached_option_chain(sym) is None
        ]
        if not misses:
            return
        max_workers = min(len(misses), 4)

        def _warm(sym: str) -> None:
            try:
                # I/O-only path: populate the symbol-keyed cache without
                # running the put/call + liquidity filter (those are
                # parameterised per build call and don't affect the cache).
                self._fetch_raw_option_chain(client, sym)
            except Exception as exc:
                self._option_chain_prefetch_failures(f"option_chain_prefetch:{sym}",
                                                     "Option chain prefetch failed for %s: %s: %s",
                                                     sym, type(exc).__name__, exc)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bot-option-chain-prefetch") as executor:
            futures = [executor.submit(_warm, sym) for sym in misses]
            for future in as_completed(futures):
                # _warm swallows its own exceptions; result() here is a
                # no-op safety check that ensures any unexpected exception
                # propagates to the caller's outer error handling.
                future.result()

    def _spread_market_failure_detail(self, first_leg: OptionContract, second_leg: OptionContract) -> str:
        bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
        max_net_spread_price = float(self.optcfg.max_net_spread_price)
        min_net_mid_price = float(self.optcfg.min_net_mid_price)
        max_net_spread_pct = float(self.optcfg.max_net_spread_pct)
        if ask <= 0 or mid <= 0:
            return detail_fields(reason="invalid_spread_market", net_bid=bid, net_ask=ask, net_mid=mid)
        if ask > max_net_spread_price:
            return detail_fields(reason="net_ask_too_high", required_max_net_ask=max_net_spread_price, current_net_ask=ask, net_bid=bid, net_mid=mid)
        if mid < min_net_mid_price:
            return detail_fields(reason="net_mid_too_low", required_min_net_mid=min_net_mid_price, current_net_mid=mid, net_bid=bid, net_ask=ask)
        spread_pct = (ask - bid) / max(mid, 0.01)
        if spread_pct > max_net_spread_pct:
            return detail_fields(reason="net_spread_pct_too_wide", required_max_net_spread_pct=max_net_spread_pct, current_net_spread_pct=spread_pct, net_bid=bid, net_ask=ask, net_mid=mid)
        return detail_fields(reason="invalid_spread_market", net_bid=bid, net_ask=ask, net_mid=mid)

    def _validate_spread_market(self, first_leg: OptionContract, second_leg: OptionContract) -> tuple[float, float, float] | None:
        """(net bid, net ask, net mid) of a tradable vertical, else None.
        A market it returns has a mid above zero (never NaN), which the
        builders' ``mark_price_hint`` reads with no fallback."""
        bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
        if ask <= 0 or mid <= 0:
            return None
        if ask > float(self.optcfg.max_net_spread_price):
            return None
        if mid < float(self.optcfg.min_net_mid_price):
            return None
        if (ask - bid) / max(mid, 0.01) > float(self.optcfg.max_net_spread_pct):
            return None
        # Structural sanity: the net price must be a sane fraction of the
        # strike width. A quote implying a credit at or above the width is
        # free money and books a max loss of zero, which then sizes to the
        # contract cap on a position whose real risk is the full width.
        width_frac_cap = float(getattr(self.optcfg, "max_net_price_frac_of_width", 0.0) or 0.0)
        if width_frac_cap > 0:
            frac = net_price_frac_of_width(first_leg, second_leg, ask)
            if frac is None or frac > width_frac_cap:
                return None
        return bid, ask, mid

    def _validate_single_option_market(self, contract: OptionContract) -> tuple[float, float, float] | None:
        """(bid, ask, mid) of a tradable single option, else None; as
        ``_validate_spread_market``, a market it returns has a mid above zero."""
        bid, ask, mid = single_option_price_bounds(contract)
        if ask <= 0 or mid <= 0:
            return None
        if ask > float(self.optcfg.max_single_option_price):
            return None
        if contract.spread_pct > float(self.optcfg.max_bid_ask_spread_pct):
            return None
        return bid, ask, mid

    def _single_option_market_failure_detail(self, contract: OptionContract) -> str:
        """Why ``_validate_single_option_market`` refused ``contract``, as
        ``_spread_market_failure_detail`` describes a vertical."""
        bid, ask, mid = single_option_price_bounds(contract)
        if ask <= 0 or mid <= 0:
            return detail_fields(reason="invalid_option_market", bid=bid, ask=ask, mid=mid)
        max_price = float(self.optcfg.max_single_option_price)
        if ask > max_price:
            return detail_fields(reason="option_ask_too_high", required_max_ask=max_price, current_ask=ask, bid=bid, mid=mid)
        return detail_fields(reason="option_spread_pct_too_wide", required_max_spread_pct=float(self.optcfg.max_bid_ask_spread_pct),
                             current_spread_pct=contract.spread_pct, bid=bid, ask=ask, mid=mid)

    @staticmethod
    def _option_quote_stability_force_cooldown_seconds() -> float:
        return 0.0

    def _stabilize_quotes(
        self,
        data,
        legs: tuple[OptionContract, ...],
        *,
        validate: Callable[..., tuple[float, float, float] | None],
        failure_detail: Callable[..., str],
        source: str,
    ) -> tuple[tuple[OptionContract, ...] | None, str | None]:
        """Re-quote the picked ``legs`` ``options.quote_stability_checks``
        times, ``quote_stability_pause_ms`` apart: each round forces a quote
        fetch, needs every leg fresh and quoted, and ``validate`` (called with
        the re-quoted legs) to pass their market; then the mid may not have
        drifted more than ``max_mid_drift_pct``. Returns the last re-quoted
        legs and None, or None and why (``reason=...,k=v``; a refused market
        described by ``failure_detail``). With no data feed the legs come back
        as picked. One loop for the verticals and the single options: until
        2026-09-27 each had a copy, and only the credit spread recorded why."""
        if data is None:
            return legs, None
        symbols = [leg.symbol for leg in legs]
        checks = max(1, int(self.optcfg.quote_stability_checks))
        mids: list[float] = []
        latest = legs
        for idx in range(checks):
            data.fetch_quotes(symbols, force=True, min_force_interval_seconds=self._option_quote_stability_force_cooldown_seconds(), source=source)
            if not data.quotes_are_fresh(symbols, self.optcfg.max_quote_age_seconds):
                return None, detail_fields(reason="quote_not_fresh", required_max_quote_age_seconds=float(self.optcfg.max_quote_age_seconds), completed_checks=idx, symbols="|".join(symbols))
            quotes = [data.get_quote(symbol) for symbol in symbols]
            if not all(quotes):
                names = ("first", "second")
                return None, detail_fields(
                    reason="missing_leg_quotes",
                    **{f"{name}_symbol": leg.symbol for name, leg in zip(names, legs)},
                    **{f"{name}_quote": bool(quote) for name, quote in zip(names, quotes)},
                    completed_checks=idx,
                )
            latest = tuple(contract_from_quote(leg.symbol, quote, asdict(leg)) for leg, quote in zip(legs, quotes))
            market = validate(*latest)
            if market is None:
                return None, failure_detail(*latest)
            mids.append(market[2])
            if idx + 1 < checks:
                time_mod.sleep(max(0.0, float(self.optcfg.quote_stability_pause_ms) / 1000.0))
        drift = (max(mids) - min(mids)) / max(mids[-1], 0.01)
        if drift > float(self.optcfg.max_mid_drift_pct):
            return None, detail_fields(reason="mid_drift_too_high", required_max_mid_drift_pct=float(self.optcfg.max_mid_drift_pct), current_mid_drift_pct=drift, checks=checks)
        return latest, None
