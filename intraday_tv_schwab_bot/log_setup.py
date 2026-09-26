# SPDX-License-Identifier: MIT
"""Process logging: the TRADEFLOW level, the handlers ``setup_logging``
installs (an ET-dated daily file and a colour console), and ``warn_once``
for warnings raised from a hot path."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import sessions

LOG = logging.getLogger(__name__)

TRADEFLOW_LEVEL = 25
logging.addLevelName(TRADEFLOW_LEVEL, "TRADEFLOW")

_WARNED: set[str] = set()


def warn_once(key: str) -> bool:
    """True the first time ``key`` is seen in this process, False after.
    For warnings raised from a hot path (per symbol, per candidate, per
    cycle): the caller logs only when this returns True. One set serves
    every module, so prefix the key with the caller's concern."""
    if key in _WARNED:
        return False
    _WARNED.add(key)
    return True


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        get_std_handle = getattr(kernel32, "GetStdHandle", None)
        get_console_mode = getattr(kernel32, "GetConsoleMode", None)
        set_console_mode = getattr(kernel32, "SetConsoleMode", None)
        if (
            not callable(get_std_handle)
            or not callable(get_console_mode)
            or not callable(set_console_mode)
        ):
            return
        handle = get_std_handle(-11)
        if not handle:
            return
        mode = ctypes.c_uint32()
        if get_console_mode(handle, ctypes.byref(mode)) == 0:
            return
        set_console_mode(handle, mode.value | 0x0004)
    except Exception:
        return


def _console_supports_color(stream: Any) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if not is_tty:
        return False
    term = str(os.getenv("TERM", "")).lower()
    if term == "dumb":
        return False
    return True


class ColorConsoleFormatter(logging.Formatter):
    RESET = "\033[0m"
    DIM = "\033[2m"
    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[37m",
        TRADEFLOW_LEVEL: "\033[95m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41;97m",
    }

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        color = self._pick_color(record)
        return f"{color}{base}{self.RESET}" if color else base

    def _pick_color(self, record: logging.LogRecord) -> str:
        message = record.getMessage().lower()
        if record.levelno >= logging.ERROR:
            return self.LEVEL_COLORS[logging.ERROR]
        if record.levelno == logging.WARNING:
            return self.LEVEL_COLORS[logging.WARNING]
        if "paper account entry recorded" in message or " action=entered" in message:
            return "\033[32m"
        if "paper account exit recorded" in message:
            return "\033[35m"
        if "entry_retry_backoff" in message or "cooldown" in message or "underlying_already_open" in message:
            return "\033[90m"
        if " not_filled" in message or "option entry attempt" in message or "exit attempt" in message:
            return "\033[33m"
        if "starting bot" in message or "dashboard listening" in message:
            return "\033[96m"
        if "candidate cycle" in message or "entry cycle" in message:
            return "\033[94m"
        if "fetching price_history" in message or "quote refresh" in message:
            return self.DIM
        return self.LEVEL_COLORS.get(record.levelno, "")


class _ETDailyFileHandler(logging.FileHandler):
    """FileHandler that rotates to ``bot_{YYYY-MM-DD}.log`` on ET-date change,
    regardless of host timezone. `session_report.export_session_archive`
    expects a filename matching the current session date. Date-check is
    throttled via a monotonic timer so the hot log path doesn't pay for a
    tz conversion on every record."""

    _CHECK_INTERVAL_SECONDS = 30.0

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        self._log_dir = Path(log_dir)
        self._current_date = sessions.now_et().date().isoformat()
        self._last_date_check = time.monotonic()
        super().__init__(self._log_dir / f"bot_{self._current_date}.log", encoding=encoding)

    def emit(self, record: logging.LogRecord) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_date_check >= self._CHECK_INTERVAL_SECONDS:
            self._last_date_check = now_mono
            today = sessions.now_et().date().isoformat()
            if today != self._current_date:
                self.acquire()
                try:
                    if today != self._current_date:
                        if self.stream is not None:
                            self.stream.close()
                        self._current_date = today
                        self.baseFilename = str(self._log_dir / f"bot_{today}.log")
                        self.stream = self._open()
                finally:
                    self.release()
        super().emit(record)


def setup_logging(log_dir: str | Path) -> None:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            LOG.debug(
                "Unable to reconfigure stdout encoding; "
                "continuing with existing stream settings.",
                exc_info=True,
            )

    file_handler = _ETDailyFileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(stream=stream)
    console_handler.setLevel(logging.INFO)
    _enable_windows_ansi()
    if _console_supports_color(stream):
        console_handler.setFormatter(ColorConsoleFormatter())
    else:
        console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("websockets.client").setLevel(logging.INFO)

    LOG.info("Logging to %s (daily rotation at ET midnight)", file_handler.baseFilename)
