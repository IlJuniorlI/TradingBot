# SPDX-License-Identifier: MIT
from __future__ import annotations

import argparse
from pathlib import Path

from intraday_tv_schwab_bot.config import available_strategy_names, load_config

_DEFAULT_CONFIG_CANDIDATES = (
    Path("configs/config.yaml"),
    Path("configs/config.example.yaml"),
)


def _default_config_path() -> str:
    """Return the best default config path available in the package."""
    for candidate in _DEFAULT_CONFIG_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return str(_DEFAULT_CONFIG_CANDIDATES[0])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradingView + Schwabdev intraday bot"
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

    config = load_config(args.config, strategy_override=args.strategy, env_path=args.env)

    from intraday_tv_schwab_bot.engine import IntradayBot

    bot = IntradayBot(config)
    bot.run()


if __name__ == "__main__":
    main()
