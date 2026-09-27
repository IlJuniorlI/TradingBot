@REM ======================================================================
@REM First-time setup (Windows):
@REM   1. Open this folder in Command Prompt or PowerShell
@REM   2. Create the virtualenv:   python -m venv .venv
@REM   3. Activate it:             .venv\Scripts\activate
@REM   4. Install dependencies:    pip install -r requirements.txt
@REM   5. Copy .env.example to .env and fill in your Schwab + TradingView creds
@REM   6. Then double-click this file (or run it from a terminal) to start the bot
@REM
@REM Arguments are passed on to main.py after --config configs\config.yaml;
@REM it runs from this folder, so relative paths resolve here:
@REM   --env C:\path\to\custom.env  override .env auto-discovery, e.g. to run
@REM                                several instances with different credentials
@REM   --strategy <name>            override the strategy the config selects
@REM   --config <path>              run another config (the last --config wins)
@REM e.g. start_trading_bot.bat --config configs\config.small_cap_squeeze.yaml
@REM
@REM Stop the bot with Ctrl+C in its window: that is its clean shutdown.
@REM Ctrl+Break stops it cleanly too (in the pause between cycles, once the
@REM pause ends). Closing the window does not: Windows ends the bot before
@REM its cleanup can finish.
@REM cmd.exe has no exec, so python runs as a child of the cmd.exe running
@REM this file, and ending that cmd.exe alone leaves the bot running.
@REM ======================================================================
cd /D "%~dp0"
call .venv\Scripts\activate
python main.py --config configs\config.yaml %*
