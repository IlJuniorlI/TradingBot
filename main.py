# SPDX-License-Identifier: MIT
"""Run the bot from a source checkout: ``python main.py --config ...``.

The command line lives in ``intraday_tv_schwab_bot.cli``; an installed
package runs the same ``main`` as the ``intraday-tv-schwab-bot`` script.
"""
from intraday_tv_schwab_bot.cli import main

if __name__ == "__main__":
    main()
