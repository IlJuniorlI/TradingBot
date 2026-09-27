#!/usr/bin/env bash
# ======================================================================
# First-time setup (Linux / macOS):
#   1. cd into this folder in a terminal
#   2. Create the virtualenv:   python3.11 -m venv .venv
#      (the bot needs Python 3.11+; a plain python3 can be older, e.g.
#      3.9 on Debian 11, and that venv cannot run it)
#   3. Activate it:             source .venv/bin/activate
#   4. Install dependencies:    pip install -r requirements.txt
#   5. Copy .env.example to .env and fill in your Schwab + TradingView creds
#   6. Make this script executable: chmod +x start_trading_bot.sh
#   7. Then run:               ./start_trading_bot.sh
#
# Arguments are passed on to main.py after --config configs/config.yaml;
# it runs from this folder, so relative paths resolve here:
#   --env /path/to/custom.env   override .env auto-discovery, e.g. to run
#                               several instances with different credentials
#   --strategy <name>           override the strategy the config selects
#   --config <path>             run another config (the last --config wins)
# e.g. ./start_trading_bot.sh --config configs/config.small_cap_squeeze.yaml
#
# The bot takes over this script's process (exec), so `kill <pid>` of the
# script is a stop signal to the bot, like Ctrl+C: it shuts down cleanly.
# ======================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source .venv/bin/activate
exec python main.py --config configs/config.yaml "$@"
