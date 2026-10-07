# SPDX-License-Identifier: MIT
"""The Schwab HTTP client wrapper: every broker call goes through here.

``call_schwab_client`` counts the call, serializes the token refresh and logs
a non-2xx response. ``response_ok`` is the one reading of a response status.
``SCHWAB_TRANSPORT_ERRORS`` are the transport failures the client raises,
and ``SCHWAB_WRITE_UNKNOWN_OUTCOME`` those an order write raises without
telling whether the broker acted on it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from schwabdev import Client as SchwabClient

from . import sessions
from .numeric import safe_int

LOG = logging.getLogger(__name__)


class SchwabdevApiUsageTracker:
    # Sliding-window rate tracker. The previous implementation reported a
    # lifetime average (total_calls / elapsed_since_start), which drifts
    # toward zero as soon as the bot enters idle hours — for an always-on
    # bot, ~11 overnight idle hours per day cut the average roughly in
    # half, even though the actual peak-hour rate hasn't changed. The
    # rate that matters for Schwab's ~120 req/min cap is "how many calls
    # right now", not "how many on average since the bot started days
    # ago". This tracker keeps a deque of recent call timestamps bounded
    # by the largest reported window (30 min) and reports per-minute
    # rates over 1m / 5m / 15m / 30m sliding windows.
    _MAX_WINDOW_SECONDS = 30 * 60

    def __init__(self) -> None:
        self.started_at = sessions.now_et()
        self.total_calls = 0
        self.last_call_at: Optional[datetime] = None
        self.method_counts: dict[str, int] = {}
        # Deque of (timestamp, method_name) tuples — pruned to the
        # _MAX_WINDOW_SECONDS horizon on every record_call/snapshot.
        # At 20 calls/min sustained, max 600 entries — trivial memory.
        self._call_times: deque[tuple[datetime, str]] = deque()
        self._lock = RLock()

    def record_call(self, method_name: str) -> None:
        name = str(method_name or 'unknown')
        now = sessions.now_et()
        with self._lock:
            self.total_calls += 1
            self.last_call_at = now
            self.method_counts[name] = int(self.method_counts.get(name, 0)) + 1
            self._call_times.append((now, name))
            self._prune(now)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._MAX_WINDOW_SECONDS)
        while self._call_times and self._call_times[0][0] < cutoff:
            self._call_times.popleft()

    def _calls_in_window(self, now: datetime, seconds: float) -> int:
        cutoff = now - timedelta(seconds=float(seconds))
        # Linear scan — deque is bounded at ~600 entries (30min × 20cpm),
        # so this is sub-microsecond. bisect would be faster but adds
        # complexity for negligible gain at this size.
        count = 0
        for ts, _ in self._call_times:
            if ts >= cutoff:
                count += 1
        return count

    def snapshot(self, now: Optional[datetime] = None) -> dict[str, Any]:
        current = now or sessions.now_et()
        with self._lock:
            self._prune(current)
            elapsed_minutes = max(
                (current - self.started_at).total_seconds() / 60.0,
                1.0 / 60.0,
            )
            lifetime_per_minute = (
                float(self.total_calls) / elapsed_minutes
                if self.total_calls > 0
                else 0.0
            )
            calls_1m = self._calls_in_window(current, 60.0)
            calls_5m = self._calls_in_window(current, 300.0)
            calls_15m = self._calls_in_window(current, 900.0)
            calls_30m = self._calls_in_window(current, 1800.0)
            # The dashboard reads `calls_per_minute_5m` — short enough to be
            # responsive (a 1-minute spike shows up by the second minute) and
            # smoothed enough not to jitter on every individual call. Each
            # window's rate divides by the time it covers: the tracker's
            # uptime until that reaches the window, at least a minute (a
            # first partial minute reads as its count). Until 2026-10-06 the
            # 5-, 15- and 30-minute rates divided by the whole window, so each
            # read low until the uptime reached it: one minute up, the
            # 5-minute rate showed the startup burst at a fifth of its pace.
            # The 1-minute rate is unchanged.
            def per_minute(calls: int, window_minutes: float) -> float:
                return float(calls) / max(1.0, min(window_minutes, elapsed_minutes))

            calls_per_minute_1m = per_minute(calls_1m, 1.0)       # last 60s
            calls_per_minute_5m = per_minute(calls_5m, 5.0)       # 5min-avg
            calls_per_minute_15m = per_minute(calls_15m, 15.0)    # 15min-avg
            calls_per_minute_30m = per_minute(calls_30m, 30.0)    # 30min-avg
            snapshot = {
                'started_at': self.started_at.isoformat(),
                "last_call_at": (
                    self.last_call_at.isoformat()
                    if self.last_call_at is not None
                    else None
                ),
                'total_calls': int(self.total_calls),
                'calls_per_minute_1m': calls_per_minute_1m,
                'calls_per_minute_5m': calls_per_minute_5m,
                'calls_per_minute_15m': calls_per_minute_15m,
                'calls_per_minute_30m': calls_per_minute_30m,
                'calls_window_1m': int(calls_1m),
                'calls_window_5m': int(calls_5m),
                'calls_window_15m': int(calls_15m),
                'calls_window_30m': int(calls_30m),
                'lifetime_calls_per_minute': lifetime_per_minute,
                'method_counts': dict(self.method_counts),
            }
        return snapshot


_SCHWAB_CLIENT_TRACKERS: dict[int, SchwabdevApiUsageTracker] = {}


def register_schwab_api_tracker(client: Any, tracker: SchwabdevApiUsageTracker) -> None:
    _SCHWAB_CLIENT_TRACKERS[id(client)] = tracker


def get_schwab_api_tracker(client: Any) -> Optional[SchwabdevApiUsageTracker]:
    return _SCHWAB_CLIENT_TRACKERS.get(id(client))


def _truncate_log_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# schwabdev's access-token refresh is not safe across threads. Its
# ``Tokens.update_tokens`` checks expiry OUTSIDE its lock, and
# ``_update_access_token`` records the last-known issue time only AFTER taking
# the lock -- so every thread queued behind the first refresh reads the NEW
# token as "last known" and refreshes again. On 2026-09-22 at 09:15 the prewarm
# fan-out produced four refreshes in two seconds. Checking the token here, one
# caller at a time, lets the first caller refresh and the rest find a fresh
# token; the request's own internal check then has nothing to do.
_TOKEN_REFRESH_LOCK = Lock()


def _refresh_token_serialized(client: Any) -> None:
    if not isinstance(client, SchwabClient):
        return
    with _TOKEN_REFRESH_LOCK:
        client.update_tokens()


# schwabdev 4.0.0 (tokens.py): a refresh token lives 7 days from its issue
# (``Tokens._refresh_token_timeout``), and once less than 3630 s of it is left
# ``Tokens.update_tokens`` starts the authorization flow inside whichever API
# call checks it next (here under _TOKEN_REFRESH_LOCK): it prints the login
# URL and waits on ``input()`` for the callback URL. A run on a terminal waits
# there; one without stdin (systemd) gets EOFError, which schwabdev logs
# ("Could not update refresh token (EOF when reading a line)") on every call.
# The check comes before the access token's, so from then the 30-minute access
# token is not renewed either and the calls fail (401) once it lapses: at
# 14:08:26 on 2026-09-25, a minute after the first EOFError.
SCHWABDEV_REFRESH_TOKEN_LIFETIME = timedelta(days=7)
SCHWABDEV_LOGIN_LEAD = timedelta(seconds=3630)


@dataclass(frozen=True, slots=True)
class RefreshTokenWindow:
    """When schwabdev's refresh token was issued (UTC), when it expires and
    when schwabdev starts its login flow for a new one."""
    issued_at: datetime

    @property
    def expires_at(self) -> datetime:
        return self.issued_at + SCHWABDEV_REFRESH_TOKEN_LIFETIME

    @property
    def login_at(self) -> datetime:
        return self.expires_at - SCHWABDEV_LOGIN_LEAD


def read_refresh_token_window(tokens_db: str) -> RefreshTokenWindow:
    """The refresh token's window, read from schwabdev's token store
    (``schwab.tokens_db``, the sqlite file its ``Tokens`` keeps; the path
    expanded as schwabdev expands it) opened read-only: no request, no
    authorization flow, nothing written or created. The issue time is stored
    in clear even when the tokens are encrypted; a naive one is UTC, as
    schwabdev reads it.

    Raises ``sqlite3.Error`` for a store it cannot open or read (a missing
    file, no ``schwabdev`` table, a lock held past the 1 s timeout while
    schwabdev renews the tokens) and ``ValueError`` for one holding no token
    or an issue time that does not parse."""
    uri = Path(os.path.expanduser(tokens_db)).absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
        row = conn.execute("SELECT refresh_token_issued FROM schwabdev LIMIT 1").fetchone()
    if row is None:
        raise ValueError("the store holds no token")
    issued = datetime.fromisoformat(str(row[0]))
    return RefreshTokenWindow(issued if issued.tzinfo is not None else issued.replace(tzinfo=timezone.utc))


def call_schwab_client(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    tracker = get_schwab_api_tracker(client)
    if tracker is not None:
        tracker.record_call(method_name)
    _refresh_token_serialized(client)
    method = getattr(client, method_name)
    response = method(*args, **kwargs)
    status = getattr(response, "status_code", None)
    if status is not None and not response_ok(response):
        code = safe_int(status)
        request = getattr(response, "request", None)
        request_method = str(getattr(request, "method", "") or "")
        request_url = str(getattr(request, "url", "") or "")
        request_path = urlsplit(request_url).path if request_url else ""
        response_body = _truncate_log_text(getattr(response, "text", ""))
        response_reason = str(getattr(response, "reason", "") or "")
        log_payload = {
            "method_name": str(method_name or "unknown"),
            "status_code": code if code is not None else str(status),
            "reason": response_reason,
            "request_method": request_method,
            "request_path": request_path,
            "args": [_truncate_log_text(arg, 120) for arg in args],
            "kwargs": {str(k): _truncate_log_text(v, 120) for k, v in kwargs.items()},
            "response_text": response_body,
        }
        if code is None or code >= 400:
            LOG.warning(
                "Schwab HTTP non-2xx response: %s",
                json.dumps(log_payload, default=str),
            )
        else:
            LOG.debug(
                "Schwab HTTP non-2xx response: %s",
                json.dumps(log_payload, default=str),
            )
    return response


def response_ok(response: Any) -> bool:
    """True for a 2xx response. A response with no status code, or one that
    cannot be read as a number, is a failure (until 2026-09-29 the latter
    raised ValueError, here and in ``call_schwab_client``'s log of it)."""
    status = safe_int(getattr(response, "status_code", None))
    return status is not None and 200 <= status < 300


class SchwabHTTPError(RuntimeError):
    """A Schwab read that did not return a 2xx response with a JSON body."""

    def __init__(self, method_name: str, status_code: Any, detail: str) -> None:
        self.method_name = str(method_name or "unknown")
        self.status_code = status_code
        super().__init__(f"{self.method_name} status={status_code} {detail}")


# The transport failures a Schwab read raises (every call_schwab_json call is
# a GET). schwabdev's Client sends it on a requests Session whose adapter
# retries a failed connect, a failed read (at most twice) and a 429, 500,
# 502, 503 or 504 status, within three retries in all, then hands back the
# last status instead of raising, which call_schwab_json reads as a
# SchwabHTTPError. A refused, reset or dropped connection, a DNS or TLS
# failure, a connect timeout and a read timeout raise requests'
# ConnectionError (ConnectTimeout and SSLError are subclasses): a timed-out
# read of the headers raises it once its retries are spent, and one of the
# body at once, never ReadTimeout. A body cut off mid-transfer raises
# ChunkedEncodingError, a body whose Content-Encoding does not decode
# ContentDecodingError, and a redirect loop TooManyRedirects (the session
# follows 30 redirects). The token refresh logs and swallows its own network
# errors. Anything else is not a transport failure, and propagates:
# schwabdev's parameter validator raises TypeError or ValueError, and
# requests raises InvalidURL, MissingSchema and the like for a request it
# cannot build. So does a redirect to a URL requests cannot use (a malformed
# Location raises ValueError, a non-http scheme InvalidSchema): the server
# supplied it, but it propagates like a bug. Checked 2026-09-28 against
# schwabdev 4.0.0, requests 2.34.2 and urllib3 2.8.0, with loopback servers
# failing each of those ways.
SCHWAB_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects,
)

# The failures an order write (place_order, a POST) raises when the request
# may have reached Schwab: the order may rest at the broker, or not. The
# adapter above retries GET, PUT and DELETE only (schwabdev's
# allowed_methods; a POST is not idempotent), so a POST whose response times
# out raises requests' ReadTimeout at once, which is a Timeout and not a
# ConnectionError: SCHWAB_TRANSPORT_ERRORS, derived for reads, does not name
# it. Every transport failure is taken the same way, a refused connection
# included: an order write never assumes it was not placed. Checked
# 2026-09-28 against schwabdev 4.0.0, requests 2.34.2 and urllib3 2.8.0 with
# a loopback server that reads the request and never answers: a POST raised
# ReadTimeout, a GET ConnectionError.
SCHWAB_WRITE_UNKNOWN_OUTCOME: tuple[type[Exception], ...] = (
    requests.exceptions.Timeout,
    *SCHWAB_TRANSPORT_ERRORS,
)


def call_schwab_json(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``method_name`` and return its decoded JSON body.

    The response must be 2xx (``response_ok``) and its body must decode.
    Anything else raises ``SchwabHTTPError``, so an error body is never read
    as an empty payload. The decoded body can be any JSON value, not only an
    object (a list, null, a string). A transport failure propagates
    unchanged, as one of ``SCHWAB_TRANSPORT_ERRORS``.
    """
    response = call_schwab_client(client, method_name, *args, **kwargs)
    status = getattr(response, "status_code", None)
    if not response_ok(response):
        raise SchwabHTTPError(method_name, status, f"body={_truncate_log_text(getattr(response, 'text', ''))}")
    try:
        return response.json()
    except Exception as exc:
        raise SchwabHTTPError(method_name, status, f"undecodable body: {exc}") from exc
