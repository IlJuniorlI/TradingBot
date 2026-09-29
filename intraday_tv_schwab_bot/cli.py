# SPDX-License-Identifier: MIT
"""The bot's command line.

``python main.py`` runs it from a source checkout; an installed package runs
it as the ``intraday-tv-schwab-bot`` console script that ``pyproject.toml``
declares. The script has to name a module inside the package: the wheel ships
only ``intraday_tv_schwab_bot``, so a root-level ``main`` is not importable
from an installed (or editable-installed) package.

A config the loader refuses, a bot that cannot be built from it, and an error
that ends the run exit with status 1, logged to the console and to the day's
log (the traceback to the log only); the run's own shutdown cleanup has run
by then (``IntradayBot.run``). Until 2026-09-28 each raised out of ``main`` as
a traceback on stderr: the log had no record of a startup refusal, and
``start_trading_bot.bat``'s window closed on it before it could be read.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import NoReturn

from . import __version__
from .config import RuntimeConfig, available_strategy_names, load_config
from .log_setup import setup_logging

LOG = logging.getLogger(__name__)

_DEFAULT_CONFIG_CANDIDATES = (
    Path("configs/config.yaml"),
    Path("configs/config.example.yaml"),
)


def _default_config_path() -> str:
    """The first default config that exists under the working directory."""
    for candidate in _DEFAULT_CONFIG_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return str(_DEFAULT_CONFIG_CANDIDATES[0])


def _exit_on_error(what: str, exc: Exception) -> NoReturn:
    """Log why the bot is stopping, ``what`` and the error with its type, at
    CRITICAL (the console and the day's log) and its traceback at DEBUG (the
    log only), then exit with status 1."""
    LOG.critical("%s: %s: %s", what, type(exc).__name__, exc)
    LOG.debug("%s: traceback", what, exc_info=exc)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradingView + Schwabdev intraday bot"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"intraday-tv-schwab-bot {__version__}",
    )
    parser.add_argument(
        "--config",
        default=_default_config_path(),
        help="Path to the YAML config file.",
    )
    parser.add_argument(
        "--strategy",
        choices=available_strategy_names(),
        help="Override the strategy selected in the config.",
    )
    parser.add_argument(
        "--env",
        default=None,
        help=(
            "Optional explicit path to a .env file (overrides auto-discovery "
            "from config dir / repo root / cwd). Useful for running multiple "
            "instances with different credentials, or for keeping the .env "
            "outside the repo."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config, strategy_override=args.strategy, env_path=args.env)
    except Exception as exc:
        # The loader refuses a config it cannot run on, naming the key. With
        # no config there is no runtime.log_dir to read: the refusal goes to
        # the default one (every shipped preset's), under the working
        # directory the start scripts set to the checkout.
        setup_logging(RuntimeConfig().log_dir)
        _exit_on_error(f"Startup refused: the config {args.config} could not be loaded", exc)

    from .engine import IntradayBot

    # IntradayBot sets up the day's log first; a strategy param or a blackout
    # file it refuses while it builds is logged there.
    try:
        bot = IntradayBot(config)
    except Exception as exc:
        _exit_on_error("Startup refused: the bot could not be built", exc)
    try:
        bot.run()
    except Exception as exc:
        _exit_on_error("The bot stopped on an error", exc)
