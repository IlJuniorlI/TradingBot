@REM ======================================================================
@REM First-time setup (Windows):
@REM   1. Open this folder in Command Prompt or PowerShell
@REM   2. Create the virtualenv:   python -m venv .venv
@REM   3. Activate it:             .venv\Scripts\activate
@REM   4. Install dependencies:    pip install -r requirements.txt
@REM   5. Copy .env.example to .env and fill in your Schwab + TradingView creds
@REM   6. Then double-click this file (or run it from a terminal) to start the bot
@REM
@REM Optional: pass --env C:\path\to\custom.env to override .env auto-discovery
@REM (useful for multi-instance setups with different credentials).
@REM ======================================================================
cd /D "%~dp0"
call .venv\Scripts\activate
python main.py --config configs\config.yaml
