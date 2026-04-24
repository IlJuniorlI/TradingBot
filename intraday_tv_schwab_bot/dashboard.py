# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import inspect
import json
import logging
import math
import re
import ssl
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # Numpy scalars and pandas Timestamps expose .item(); recurse through
    # _json_safe so the float/NaN guard catches NaN-valued numpy scalars that
    # would otherwise slip into json.dumps(..., allow_nan=False) and raise
    # ValueError on the serving thread.
    try:
        item = getattr(value, "item", None)
        if callable(item):
            return _json_safe(item())
    except Exception:
        LOG.debug("Failed to coerce dashboard JSON value via item(); falling back to recursive serialization.", exc_info=True)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _json_dumps_compact(value: Any) -> str:
    return json.dumps(value, default=str, allow_nan=False, separators=(",", ":"))


def _disk_state_signature(value: Any) -> str:
    def _normalize(node: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            normalized: dict[str, Any] = {}
            for key in sorted(str(k) for k in node.keys()):
                if path == () and key == 'last_update':
                    continue
                if path == ('api_usage',) and key == 'avg_calls_per_minute':
                    continue
                normalized[key] = _normalize(node[key], path + (key,))
            return normalized
        if isinstance(node, list):
            return [_normalize(item, path) for item in node]
        return node

    return json.dumps(_normalize(value), default=str, allow_nan=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=1)
def _image_assets() -> dict[str, str]:
    raw = json.loads(_dashboard_asset_text("images.json"))
    if not isinstance(raw, dict):
        raise ValueError("dashboard image asset manifest must be a JSON object")
    return {str(key): str(value) for key, value in raw.items()}


@lru_cache(maxsize=1)
def _image_assets_json() -> str:
    return _json_dumps_compact(_image_assets())


@lru_cache(maxsize=1)
def _brand_badge_data_uri() -> str:
    return _image_assets().get("brand_badge", "")


_DASHBOARD_ASSETS_DIR = Path(__file__).with_name("dashboard_assets")
_THEME_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")
# Whitelist of extensions a theme may ship under its assets/ folder. Anything
# outside this set is rejected at the route layer — keeps .py / .html / random
# binaries from being served under a theme asset URL.
_THEME_ASSET_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def _resolve_asset_path(name: str) -> Path:
    resolved = (_DASHBOARD_ASSETS_DIR / name).resolve()
    if not resolved.is_relative_to(_DASHBOARD_ASSETS_DIR.resolve()):
        raise ValueError(f"Dashboard asset path traversal blocked: {name!r}")
    return resolved


@lru_cache(maxsize=None)
def _dashboard_asset_text(name: str) -> str:
    return _resolve_asset_path(name).read_text(encoding="utf-8")


@lru_cache(maxsize=256)
def _dashboard_asset_binary_bytes(name: str) -> bytes:
    return _resolve_asset_path(name).read_bytes()


def _theme_asset_text_if_present(theme: str, name: str) -> str | None:
    """Return the text of dashboard_assets/themes/<theme>/<name> or None if missing."""
    if not _THEME_NAME_PATTERN.match(theme):
        return None
    try:
        return _dashboard_asset_text(f"themes/{theme}/{name}")
    except (OSError, ValueError):
        return None


def _resolve_theme_name(theme: str | None) -> str:
    """Normalize the requested theme name and fall back to 'default' if unusable.

    Falls back (with a warning) when the name is malformed or its folder does
    not exist under dashboard_assets/themes/. The result is always a name we
    know we can serve cleanly; the rest of the render pipeline can trust it.
    """
    requested = str(theme or "default").strip().lower()
    if requested == "default":
        return "default"
    if not _THEME_NAME_PATTERN.match(requested):
        LOG.warning("Dashboard theme %r has an invalid name — falling back to 'default'.", requested)
        return "default"
    if not (_DASHBOARD_ASSETS_DIR / "themes" / requested).is_dir():
        LOG.warning("Dashboard theme %r not found under %s — falling back to 'default'.", requested, _DASHBOARD_ASSETS_DIR / "themes")
        return "default"
    return requested


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class DashboardState:
    def __init__(self):
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "status": "starting",
            "message": "Dashboard booting",
        }
        self._serialized_json: bytes | None = None

    def update(self, payload: dict[str, Any], serialized_json: str | None = None) -> None:
        with self._lock:
            self._state = copy.deepcopy(payload)
            self._serialized_json = serialized_json.encode("utf-8") if isinstance(serialized_json, str) else None

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def get_serialized(self) -> bytes | None:
        with self._lock:
            return bytes(self._serialized_json) if self._serialized_json is not None else None


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        refresh_ms: int = 2000,
        state_path: str | None = None,
        theme: str = "default",
        https: bool = False,
        ssl_certfile: str = "",
        ssl_keyfile: str = "",
        chart_payload_provider: Callable[..., dict[str, Any]] | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.refresh_ms = int(refresh_ms)
        self.state_path = Path(state_path).expanduser() if state_path else None
        self.theme = _resolve_theme_name(theme)
        self.https = bool(https)
        self.ssl_certfile = str(ssl_certfile or "").strip()
        self.ssl_keyfile = str(ssl_keyfile or "").strip()
        self.state = DashboardState()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        scheme = "https" if self.https else "http"
        self.url = f"{scheme}://{self.host}:{self.port}"
        self._last_state_signature: str | None = None
        self.chart_payload_provider = chart_payload_provider

    @staticmethod
    def _call_chart_payload_provider(provider: Callable[..., dict[str, Any]], symbol: str, max_bars: int, timeframe_mode: str = '1m') -> dict[str, Any]:
        try:
            signature = inspect.signature(provider)
        except (TypeError, ValueError):
            return provider(symbol, max_bars=max_bars, timeframe_mode=timeframe_mode)

        parameters = list(signature.parameters.values())
        supports_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters)
        kwargs: dict[str, Any] = {}
        if supports_kwargs or 'max_bars' in signature.parameters:
            kwargs['max_bars'] = max_bars
        if supports_kwargs or 'timeframe_mode' in signature.parameters:
            kwargs['timeframe_mode'] = timeframe_mode
        if kwargs:
            return provider(symbol, **kwargs)

        positional_params = [
            param
            for param in parameters
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters)
        if accepts_varargs or len(positional_params) >= 3:
            return provider(symbol, max_bars, timeframe_mode)
        if accepts_varargs or len(positional_params) >= 2:
            return provider(symbol, max_bars)
        return provider(symbol)

    def start(self) -> None:
        if self.httpd is not None:
            return
        handler = self._make_handler()
        self.httpd = ReusableThreadingHTTPServer((self.host, self.port), handler)
        if self.https:
            if not self.ssl_certfile:
                raise ValueError("dashboard.https is enabled but dashboard.ssl_certfile is not set")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(
                certfile=self.ssl_certfile,
                keyfile=self.ssl_keyfile or None,
            )
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = Thread(target=self.httpd.serve_forever, name="dashboard-server", daemon=True)
        self.thread.start()
        LOG.info("Dashboard listening at %s", self.url)

    def stop(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None

    def publish(self, payload: dict[str, Any]) -> None:
        # Guard the whole body: an exception here (exotic payload, disk write
        # failure) would propagate into the engine thread and kill the cycle.
        try:
            safe_payload = _json_safe(payload)
            serialized = _json_dumps_compact(safe_payload)
            self.state.update(safe_payload, serialized)
            if not self.state_path:
                return
            state_signature = _disk_state_signature(safe_payload)
            if state_signature == self._last_state_signature:
                return
            pretty_serialized = json.dumps(safe_payload, indent=2, default=str, allow_nan=False)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp_path.write_text(pretty_serialized, encoding="utf-8")
            tmp_path.replace(self.state_path)
            self._last_state_signature = state_signature
        except Exception as exc:
            LOG.warning("Dashboard publish failed: %s", exc, exc_info=True)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self.state
        refresh_ms = self.refresh_ms
        theme = self.theme
        chart_payload_provider = self.chart_payload_provider

        class Handler(BaseHTTPRequestHandler):
            def _security_headers(self) -> None:
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")

            def _write_json(self, payload_obj: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
                payload = _json_dumps_compact(_json_safe(payload_obj)).encode("utf-8")
                self._write_json_bytes(payload, status)

            def _write_json_bytes(self, payload: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self._security_headers()
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    LOG.info(
                        "Dashboard client disconnected while sending %s to %s: %s",
                        self.path,
                        self.client_address[0] if self.client_address else "unknown",
                        exc,
                    )

            def log_message(self, fmt: str, *args) -> None:
                LOG.debug("Dashboard %s - %s", self.address_string(), fmt % args)

            def _serve_static(self, asset_name: str, content_type: str) -> None:
                """Send a static asset from dashboard_assets/ with security headers."""
                try:
                    payload = _dashboard_asset_binary_bytes(asset_name)
                except (OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self._security_headers()
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    LOG.info(
                        "Dashboard client disconnected while sending %s to %s: %s",
                        self.path,
                        self.client_address[0] if self.client_address else "unknown",
                        exc,
                    )

            def _serve_html(self, body: bytes) -> None:
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self._security_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    LOG.info(
                        "Dashboard client disconnected while sending %s to %s: %s",
                        self.path,
                        self.client_address[0] if self.client_address else "unknown",
                        exc,
                    )

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/assets/dashboard.css":
                    self._serve_static("dashboard.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/assets/dashboard.js":
                    self._serve_static("dashboard.js", "application/javascript; charset=utf-8")
                    return
                if parsed.path == "/assets/mobile.css":
                    self._serve_static("mobile.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/assets/mobile.js":
                    self._serve_static("mobile.js", "application/javascript; charset=utf-8")
                    return
                if parsed.path.startswith("/themes/"):
                    # Routes this branch handles:
                    #   /themes/<name>/theme.css       → per-theme token/style override
                    #   /themes/<name>/theme.js        → per-theme JS hook (optional)
                    #   /themes/<name>/assets/<path>   → theme-owned images/fonts/etc.
                    rest = parsed.path[len("/themes/"):]
                    slash = rest.find("/")
                    if slash < 0:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    theme_name = rest[:slash]
                    sub_path = rest[slash + 1:]
                    if not _THEME_NAME_PATTERN.match(theme_name):
                        self.send_error(HTTPStatus.BAD_REQUEST)
                        return
                    if sub_path == "theme.css":
                        self._serve_static(f"themes/{theme_name}/theme.css", "text/css; charset=utf-8")
                        return
                    if sub_path == "theme.js":
                        self._serve_static(f"themes/{theme_name}/theme.js", "application/javascript; charset=utf-8")
                        return
                    if sub_path.startswith("assets/"):
                        asset_rel = sub_path[len("assets/"):]
                        if not asset_rel or asset_rel.endswith("/"):
                            self.send_error(HTTPStatus.NOT_FOUND)
                            return
                        ext = Path(asset_rel).suffix.lower()
                        content_type = _THEME_ASSET_MIME.get(ext)
                        if content_type is None:
                            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                            return
                        self._serve_static(f"themes/{theme_name}/assets/{asset_rel}", content_type)
                        return
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if parsed.path in ("/mobile", "/mobile/", "/m", "/m/"):
                    body = _mobile_html(refresh_ms, theme=theme).encode("utf-8")
                    self._serve_html(body)
                    return
                if parsed.path.startswith("/api/state"):
                    cached_payload = state.get_serialized()
                    if cached_payload is not None:
                        self._write_json_bytes(cached_payload)
                    else:
                        self._write_json(state.get())
                    return
                if parsed.path.startswith("/api/chart"):
                    if chart_payload_provider is None:
                        self._write_json({"error": "chart provider unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
                        return
                    params = parse_qs(parsed.query or "")
                    symbol = str((params.get("symbol") or [""])[0] or "").upper().strip()
                    if not symbol:
                        self._write_json({"error": "symbol is required"}, HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        requested_bars = int((params.get("bars") or [90])[0])
                    except Exception:
                        requested_bars = 90
                    requested_timeframe_mode = str((params.get("timeframe") or ['1m'])[0] or '1m').strip().lower()
                    if requested_timeframe_mode != 'htf':
                        requested_timeframe_mode = '1m'
                    capped_bars = max(1, min(requested_bars, 480))
                    try:
                        chart_payload = DashboardServer._call_chart_payload_provider(chart_payload_provider, symbol, capped_bars, requested_timeframe_mode)
                    except Exception as exc:
                        LOG.exception("Dashboard chart payload failed for %s", symbol)
                        self._write_json({"error": str(exc), "symbol": symbol}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    self._write_json(chart_payload)
                    return
                if parsed.path.startswith("/health"):
                    self._write_json({"ok": True})
                    return
                body = _html(refresh_ms, theme=theme).encode("utf-8")
                self._serve_html(body)

        return Handler


def _dashboard_html_template(theme: str) -> str:
    """Return the desktop HTML template, preferring themes/<theme>/index.html if present."""
    override = _theme_asset_text_if_present(theme, "index.html")
    if override is not None:
        return override
    return _dashboard_asset_text("dashboard.html")


def _mobile_html_template(theme: str) -> str:
    """Return the mobile HTML template, preferring themes/<theme>/mobile.html if present."""
    override = _theme_asset_text_if_present(theme, "mobile.html")
    if override is not None:
        return override
    return _dashboard_asset_text("mobile.html")


def _apply_template_substitutions(template: str, refresh_ms: int, theme: str) -> str:
    image_assets_json = _image_assets_json()
    brand_badge_data_uri = _brand_badge_data_uri()
    return (
        template
        .replace("__REFRESH_MS__", str(int(refresh_ms)))
        .replace("__IMAGES__", image_assets_json)
        .replace("__BRAND_BADGE__", brand_badge_data_uri)
        .replace("__THEME__", str(theme or "default").strip().lower())
    )


def _html(refresh_ms: int, theme: str = "default") -> str:
    return _apply_template_substitutions(_dashboard_html_template(theme), refresh_ms, theme)


def _mobile_html(refresh_ms: int, theme: str = "default") -> str:
    return _apply_template_substitutions(_mobile_html_template(theme), refresh_ms, theme)


__all__ = ["DashboardServer"]
