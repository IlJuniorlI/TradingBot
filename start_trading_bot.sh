#!/usr/bin/env bash
# ======================================================================
# First-time setup (Linux / macOS):
#   1. cd into this folder in a terminal
#   2. Create the virtualenv:   python3 -m venv .venv
#   3. Activate it:             source .venv/bin/activate
#   4. Install dependencies:    pip install -r requirements.txt
#   5. Copy .env.example to .env and fill in your Schwab + TradingView creds
#   6. Make this script executable: chmod +x start_trading_bot.sh
#   7. Then run:               ./start_trading_bot.sh
#
# Optional: pass --env /path/to/custom.env to override .env auto-discovery
# (useful for multi-instance setups with different credentials).
# ======================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source .venv/bin/activate
python main.py --config configs/config.yaml
