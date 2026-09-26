# SPDX-License-Identifier: MIT
"""The Schwab HTTP client wrapper: every broker call goes through here.

``call_schwab_client`` counts the call, serializes the token refresh and logs
a non-2xx response. ``response_ok`` is the one reading of a response status.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta
from threading import Lock, RLock
from typing import Any, Optional
from urllib.parse import urlsplit

from schwabdev import Client as SchwabClient

from .utils import now_et

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
        self.started_at = now_et()
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
        now = now_et()
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
        current = now or now_et()
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
            # smoothed enough not to jitter on every individual call.
            calls_per_minute_1m = float(calls_1m)               # last 60s
            calls_per_minute_5m = float(calls_5m) / 5.0         # 5min-avg
            calls_per_minute_15m = float(calls_15m) / 15.0      # 15min-avg
            calls_per_minute_30m = float(calls_30m) / 30.0      # 30min-avg
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


def call_schwab_client(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    tracker = get_schwab_api_tracker(client)
    if tracker is not None:
        tracker.record_call(method_name)
    _refresh_token_serialized(client)
    method = getattr(client, method_name)
    response = method(*args, **kwargs)
    status = getattr(response, "status_code", None)
    if status is not None and not response_ok(response):
        request = getattr(response, "request", None)
        request_method = str(getattr(request, "method", "") or "")
        request_url = str(getattr(request, "url", "") or "")
        request_path = urlsplit(request_url).path if request_url else ""
        response_body = _truncate_log_text(getattr(response, "text", ""))
        response_reason = str(getattr(response, "reason", "") or "")
        log_payload = {
            "method_name": str(method_name or "unknown"),
            "status_code": int(status),
            "reason": response_reason,
            "request_method": request_method,
            "request_path": request_path,
            "args": [_truncate_log_text(arg, 120) for arg in args],
            "kwargs": {str(k): _truncate_log_text(v, 120) for k, v in kwargs.items()},
            "response_text": response_body,
        }
        if int(status) >= 400:
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
    """True for a 2xx response. A response with no status code is a failure."""
    status = getattr(response, "status_code", None)
    return status is not None and 200 <= int(status) < 300


class SchwabHTTPError(RuntimeError):
    """A Schwab read that did not return a 2xx response with a JSON body."""

    def __init__(self, method_name: str, status_code: Any, detail: str) -> None:
        self.method_name = str(method_name or "unknown")
        self.status_code = status_code
        super().__init__(f"{self.method_name} status={status_code} {detail}")


def call_schwab_json(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``method_name`` and return its decoded JSON body.

    The response must be 2xx (``response_ok``) and its body must decode.
    Anything else raises ``SchwabHTTPError``, so an error body is never read
    as an empty payload. Network errors from the client propagate unchanged.
    """
    response = call_schwab_client(client, method_name, *args, **kwargs)
    status = getattr(response, "status_code", None)
    if not response_ok(response):
        raise SchwabHTTPError(method_name, status, f"body={_truncate_log_text(getattr(response, 'text', ''))}")
    try:
        return response.json()
    except Exception as exc:
        raise SchwabHTTPError(method_name, status, f"undecodable body: {exc}") from exc
