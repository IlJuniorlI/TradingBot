# Linux Deployment — systemd user service

How to run `intraday-tv-schwab-bot` on a headless Linux server as a long-lived
service that auto-restarts on crash, survives reboots, and integrates cleanly
with the bot's existing `SIGTERM → KeyboardInterrupt` clean-shutdown path.

The recommended setup is a **systemd user service**: no root needed, lives in
`~/.config/systemd/user/`, easy to version-control alongside the bot, and uses
the same auto-restart semantics as a system service.

---

## At a glance

```
~/TradingBot/                                # working directory
├── .venv/                                    # Python 3.11 venv
├── .env                                      # secrets (chmod 600)
├── .logs/                                    # daily log + per-day archives
├── .schwabdev/tokens.db                      # OAuth tokens
├── configs/config.yaml                       # runtime config
├── intraday_tv_schwab_bot/                   # bot package
└── ...

~/.config/systemd/user/intraday-bot.service   # unit file
```

Once configured, ops looks like this:

```bash
systemctl --user start intraday-bot          # start
systemctl --user status intraday-bot         # check
journalctl --user -u intraday-bot -f         # tail logs
systemctl --user restart intraday-bot        # apply config changes
```

---

## Prerequisites

- **Python 3.11 (exact minimum)**. Two reasons it can't be lower:
  - The bot codebase uses `@dataclass(slots=True)` and PEP 604 union
    types in non-stringified annotations — both require 3.10+.
  - **`schwabdev` itself requires Python 3.11**, so 3.10 won't install
    the API client at all.

  Anything in the 3.11.x patch range works. 3.12+ is fine too if your
  distro ships it.
- **Linux with systemd**: any modern distro qualifies — Debian 11+,
  Ubuntu 20.04+, RHEL/Rocky/Alma 8+, Fedora 34+, etc.
- **An existing Schwab account** with API access (developer portal app + a
  populated `app_key` / `app_secret` / `account_hash`).
- **A TradingView account** with a captured `sessionid` cookie if you use
  the screener (most strategies need it).

### Python 3.11 per distro

| Distro | Default Python | How to get 3.11 |
|---|---|---|
| Ubuntu 24.04 LTS | 3.12 | already installed (3.12 satisfies "3.11+") |
| Debian 12 (bookworm) | 3.11 | already installed |
| Ubuntu 22.04 LTS | 3.10 | deadsnakes PPA → `apt install python3.11 python3.11-venv python3.11-dev` |
| Ubuntu 20.04 | 3.8 | deadsnakes PPA → `apt install python3.11 python3.11-venv python3.11-dev` |
| RHEL/Rocky/Alma 9 | 3.9 | `dnf install python3.11 python3.11-pip` |
| RHEL/Rocky/Alma 8 | 3.6 | `dnf install python3.11 python3.11-pip` |
| Debian 11 (bullseye) | 3.9 | **build from source** (instructions below); backports does NOT carry 3.11 |
| Fedora 34+ | 3.10–3.13 | already 3.11+, or `dnf install python3.11` |

> ⚠️ **Debian 11 users**: don't waste time on `bullseye-backports` —
> it ships only Python 3.9. Same with the Ubuntu 22.04 default repos
> (3.10 only). For both you need either deadsnakes (Ubuntu) or a
> source build (Debian), or upgrade to bookworm (Debian 12).

For Ubuntu via deadsnakes:
```bash
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

For Debian 11 (bullseye) — must build from source (≈ 20–40 min on a small VPS):
```bash
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev \
    libgdbm-dev libgdbm-compat-dev libxml2-dev libxmlsec1-dev uuid-dev \
    tk-dev pkg-config xz-utils wget
cd /tmp
wget https://www.python.org/ftp/python/3.11.10/Python-3.11.10.tar.xz
tar -xf Python-3.11.10.tar.xz && cd Python-3.11.10
./configure --prefix=/usr/local --enable-optimizations --with-lto --with-ensurepip=install
make -j$(nproc)
sudo make altinstall   # installs as python3.11, leaves /usr/bin/python3 untouched
```

After install, verify:
```bash
python3.11 --version    # Python 3.11.x
python3.11 -c "import ssl, sqlite3, lzma, ctypes; print('ok')"
```

`_ssl` and `_sqlite3` are the two stdlib modules the bot can't run without.
Missing either means you're missing `libssl-dev` / `libsqlite3-dev` — install
and rebuild Python before continuing.

---

## One-time bot setup

```bash
# 1. Clone / copy the repo to ~/TradingBot
cd ~
git clone <your repo URL> TradingBot
cd TradingBot

# 2. Create the venv with Python 3.11
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. Sanity-check
.venv/bin/python -c "import intraday_tv_schwab_bot.engine; print('ok')"
```

### Create your runtime config

```bash
cp configs/config.example.yaml configs/config.yaml
# edit configs/config.yaml — pick your strategy preset, set dashboard host/port,
# tune risk caps, etc.
```

If you want to start from a tuned preset instead of the bare example:
```bash
cp configs/config.peer_confirmed_key_levels.yaml configs/config.yaml
# edit
```

### Set up `.env` with credentials

```bash
cp .env.example .env
chmod 600 .env       # critical — contains API secrets
```

Fill in:
```
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_ACCOUNT_HASH=...
SCHWAB_ENCRYPTION_KEY=...   # OR leave blank for no encryption
TRADINGVIEW_SESSIONID=...
```

Generate a Fernet encryption key (if you want one):
```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output as SCHWAB_ENCRYPTION_KEY
```

### First-time Schwab OAuth

This is the trickiest part of headless deploy. schwabdev needs a browser
once to complete the OAuth flow — and on a headless server there's no
browser. Worse, schwabdev's `webbrowser.open()` call lets the
spawned `xdg-open` subprocess steal Ctrl+C, so the wedged terminal can
only be killed from another shell.

**Easiest path: do auth on a desktop machine, then SCP the resulting
SQLite file to the server.** The tokens are bound to your Schwab
account, not the machine, so they work as-is on the server.

```bash
# On your desktop (Windows / macOS / Linux with browser):
cd ~/TradingBot
.venv/bin/python main.py --config configs/config.yaml
# Browser opens, you log in, paste the redirected URL back, tokens.db is written.
# Ctrl+C once tokens are saved.

# Then SCP to the server:
scp .schwabdev/tokens.db <user>@<server>:~/TradingBot/.schwabdev/

# Important: the SCHWAB_ENCRYPTION_KEY values must MATCH between machines.
# Copy the same value into ~/TradingBot/.env on the server.
```

If you really need to do auth from the server itself, see the
"Troubleshooting" section below for SSH X11 forwarding and pure-terminal
paste flows.

---

## The systemd unit file

Create `~/.config/systemd/user/intraday-bot.service`:

```ini
[Unit]
Description=Intraday TV + Schwab Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/TradingBot
ExecStart=%h/TradingBot/.venv/bin/python -m intraday_tv_schwab_bot.main --config configs/config.yaml

# Clean shutdown: engine.py routes SIGTERM through KeyboardInterrupt so the
# bot writes its session report + reconcile metadata + daily archive before
# exiting. Give it 30s, then SIGKILL if it's still hung.
KillSignal=SIGTERM
TimeoutStopSec=30s

# Auto-restart on crash. RestartSec=30 prevents tight-looping on a flapping
# Schwab API or expired TV session — gives you a chance to fix the .env
# without watching the bot crash 100 times a minute.
Restart=on-failure
RestartSec=30s

# Force unbuffered Python output so journalctl -f sees logs in real time.
# StreamHandler.emit() already flushes per record, but this kills the OS-level
# block buffer between Python and journald.
Environment=PYTHONUNBUFFERED=1

# Stream stdout/stderr to the journal. The bot ALSO writes its own
# .logs/bot_YYYY-MM-DD.log via _ETDailyFileHandler (the canonical record);
# journal is just a backup + lets you tail from any shell.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=intraday-bot

# Soft resource caps. Bot's prune_inactive_symbols + dashboard cache size
# limits keep RSS well under 1GB in practice; cap at 2G as a safety net.
# NOFILE bump is for the dashboard server's threading + many symbols.
LimitNOFILE=4096
MemoryMax=2G

[Install]
WantedBy=default.target
```

A few of these values to know about:

- **`%h`** expands to `$HOME` for the user — keeps the unit portable
  if you symlink your repo elsewhere later.
- **`Type=simple`** is correct for our case (the bot is a foreground
  process that doesn't fork). `Type=notify` would require the bot to
  send sd_notify signals; we don't.
- **`Restart=on-failure`** restarts on crash but NOT on clean exit
  (Ctrl+C / `systemctl stop`). If you want restart on any exit, use
  `Restart=always` — but that re-starts after a clean `auto_exit_after_session`
  too, which is usually not what you want.
- **`MemoryMax=2G`** is a soft cap. Hitting it triggers OOM-kill. Bump if
  you watch RSS stay near the limit (`systemctl --user status intraday-bot`
  shows current memory).

---

## Enable and start

```bash
# Reload, enable on next login, start now
systemctl --user daemon-reload
systemctl --user enable intraday-bot.service
systemctl --user start intraday-bot.service

# CRITICAL: keep services running after you log out / SSH disconnects.
# Without this, systemd-user kills your services on logout.
sudo loginctl enable-linger $USER

# Verify
systemctl --user status intraday-bot
```

The status output should show `active (running)` and the bot's startup
log lines. If it's `failed`, the journal has the traceback:
```bash
journalctl --user -u intraday-bot -n 100 --no-pager
```

---

## Common operations

```bash
# Live tail (journald)
journalctl --user -u intraday-bot -f

# Live tail (bot's own daily log file — usually richer, has TRADEFLOW level)
tail -f ~/TradingBot/.logs/bot_$(TZ=America/New_York date +%F).log

# Today's daily archive (after 8pm ET)
ls ~/TradingBot/.logs/sessions/$(TZ=America/New_York date +%F)/

# Status snapshot
systemctl --user status intraday-bot

# Stop cleanly (writes session report, archives day)
systemctl --user stop intraday-bot

# Restart (e.g. after editing config.yaml)
systemctl --user restart intraday-bot

# Disable autostart (leaves running until next reboot)
systemctl --user disable intraday-bot

# Edit config + reload
$EDITOR ~/TradingBot/configs/config.yaml
systemctl --user restart intraday-bot
```

---

## Accessing the dashboard remotely

The bot's dashboard ships with **no authentication**. Default config binds
to `127.0.0.1:8765`. Don't change this unless you know what you're doing.

### Option A — SSH local port forward (recommended)

From your laptop:
```bash
ssh -L 8765:localhost:8765 <user>@<server>
# then open http://localhost:8765 in your browser
# (or https://localhost:8765 if dashboard.https is true — see Option C)
```

Works from anywhere you can SSH from. Mobile too via Termius / Blink Shell.

### Option B — Tailscale / WireGuard mesh

Put the server on a private mesh network and bind the dashboard to the
mesh IP:
```yaml
# in configs/config.yaml
dashboard:
  host: 100.x.y.z   # your tailscale IP
  port: 8765
```

### Option C — In-bot HTTPS (single-user remote access)

For dashboard access from a single user without an SSH tunnel, the bot
can terminate TLS itself. Both `ssl_certfile` and `ssl_keyfile` are
required when `https: true`.

Generate a self-signed cert with `openssl` (one-time, valid ~2 years):
```bash
sudo mkdir -p /etc/trading-bot/ssl
cd /etc/trading-bot/ssl
openssl req -x509 -newkey rsa:4096 -sha256 -days 825 \
    -nodes -keyout dashboard.key -out dashboard.crt \
    -subj "/CN=trading-bot" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:$(hostname -I | awk '{print $1}')"
sudo chown "$USER:$USER" dashboard.*
sudo chmod 600 dashboard.key
sudo chmod 644 dashboard.crt
```

The SAN list MUST include every hostname or IP you'll type into the
browser bar. The example above covers loopback (`127.0.0.1`) and the
server's first LAN IP. Add more `IP:<addr>` or `DNS:<name>` entries
for additional access patterns — the browser rejects the cert if the
URL doesn't match a SAN entry.

Then in `configs/config.yaml`:
```yaml
dashboard:
  host: <your-lan-ip>   # e.g. 192.168.x.y, or 0.0.0.0 for any interface
  port: 8765
  https: true
  ssl_certfile: /etc/trading-bot/ssl/dashboard.crt
  ssl_keyfile:  /etc/trading-bot/ssl/dashboard.key
```

The browser warns about the self-signed cert on first visit — click
Advanced → Proceed. Subsequent loads are warning-free until the cert
expires.

The dashboard uses HTTP/1.1 keep-alive and offloads TLS handshakes to
per-request worker threads, so polling and asset fetches share one
connection and there's no first-load stall under HTTPS.

For real (browser-trusted) certs without warnings, install
[`mkcert`](https://github.com/FiloSottile/mkcert) on the machine you
browse from, generate certs there, and `scp` them to the server.
mkcert installs a local CA into your browser's trust store, so its
certs are accepted with no warning.

### Don't do this

```yaml
dashboard:
  host: 0.0.0.0   # ← public-internet exposed
```

The dashboard exposes positions, equity, watchlist, trades, and last
update timestamps. Anyone scanning your IP can read all of that —
HTTPS protects the transport but does not authenticate the visitor.
If you really need cross-network access, use Tailscale (Option B) or
in-bot HTTPS bound to a private interface (Option C). Public-internet
exposure requires a real auth layer in front — Caddy or nginx with
basic auth, Cloudflare Access, or oauth2-proxy.

---

## Operational notes

### Clock sync is critical

The bot computes ET session boundaries via `now_et()`. A clock that's
even a minute off can miss the 7am ET stream-open boundary.

```bash
systemctl status systemd-timesyncd      # should be active (running)
timedatectl                             # System clock synchronized: yes
```

If it's not active, install and enable:
```bash
sudo apt install -y systemd-timesyncd   # Debian/Ubuntu
sudo systemctl enable --now systemd-timesyncd

# Or chrony if you prefer:
sudo apt install -y chrony
sudo systemctl enable --now chrony
```

### Log rotation is automatic

`_ETDailyFileHandler` (in `utils.py`) rolls `.logs/bot_YYYY-MM-DD.log` at
midnight ET, every 30s check during emit. **Don't add `logrotate` for
that file** — it'll race with the bot's open handle. journald has its
own rotation governed by `/etc/systemd/journald.conf`.

### Daily session archives

`_maybe_export_session_archive` writes a per-day bundle to
`.logs/sessions/{YYYY-MM-DD}/` at 8pm ET each trading day. No cron needed.
Each archive contains:

- `bars/{Nm}/{SYMBOL}.csv` — full merged frame with indicators per timeframe
- `trades.csv` — today's trades only
- `bot_YYYY-MM-DD.log` — copy of the daily log
- `events.jsonl` — structured events extracted from the log
- `decisions.csv` — every entry decision as queryable rows
- `account_snapshot.json` — equity curve, realized PnL, etc.
- `config_snapshot.yaml` — resolved config (secrets redacted)
- `manifest.json` — strategy + summary stats

Disable globally with `runtime.export_session_archive: false` in your
config if you're tight on disk.

### Session reconcile on resume

With `runtime.session_reconcile_on_resume: true` (the default), the bot
re-syncs broker positions on the first cycle of each new ET trading day.
Combined with `runtime.startup_reconcile_mode: restore_hybrid`, a crash +
systemd-restart anywhere overnight cleanly recovers tracked positions on
resume — even if the user closed positions via the Schwab app while the
bot was down.

### Schwab token refresh window

The Schwab refresh token is good for **7 days**. As long as the bot makes
at least one API call within that window, the refresh token rotates
automatically. If you stop the bot for >7 days, the refresh token expires
and you have to redo the OAuth flow on a desktop, then SCP the new
`tokens.db` over.

Set a calendar reminder to either:
- run the bot at least once a week, OR
- redo the OAuth flow weekly

If you ever see `RefreshTokenExpiredError` in the logs, that's the symptom.

### TradingView session expiry

The TV `sessionid` cookie expires periodically (usually weeks to months).
When it does, the screener stops returning candidates and the bot logs
warnings. With `Restart=on-failure`, the bot will keep retrying — but it
won't actually fix itself. Watch:

```bash
journalctl --user -u intraday-bot -p warning -f
```

When you see screener failures, refresh the cookie in your `.env` and
`systemctl --user restart intraday-bot`.

### Updating the bot

```bash
cd ~/TradingBot
git pull
.venv/bin/pip install -r requirements.txt   # if requirements changed
systemctl --user restart intraday-bot
```

The clean-shutdown path runs first (writes session report + archive),
then the new code starts. Any open positions are preserved via the
reconcile metadata SQLite store.

---

## Quicker alternatives (not recommended for production)

| Approach | Use case | Why not for production |
|---|---|---|
| `tmux new -d -s bot 'cd ~/TradingBot && .venv/bin/python -m intraday_tv_schwab_bot.main --config configs/config.yaml'` | Quick one-off testing | No auto-restart on crash; doesn't survive reboot |
| `nohup ... &` + `disown` | Throwaway run | Loses stderr, no clean shutdown signal |
| Docker / Podman | Already a container shop | Extra layer; bot already isolated via venv |
| `supervisord` | Older systems without systemd | systemd is everywhere on modern Linux |

For dev and one-off testing, `tmux` is fine — `Ctrl+B` then `&` kills the
pane immediately. For anything you want to leave running across days,
use the systemd unit above.

---

## Troubleshooting

### "Access denied" message during first auth + can't Ctrl+C

schwabdev's `webbrowser.open()` spawns a subprocess (xdg-open or browser
binary) that steals the foreground process group's signals. The Python
parent is sitting at `input()` waiting for a paste — but Ctrl+C goes to
the subprocess, not Python. Symptom: terminal wedged, Ctrl+C does
nothing.

**Recovery:** kill from another shell:
```bash
kill -9 $(pgrep -f intraday_tv_schwab_bot)
```

**Don't fight this — sidestep it.** Auth on a machine that has a
browser, SCP the `tokens.db` over (see "First-time Schwab OAuth"
above). The encryption key in `.env` must match between machines.

### Bot starts in shell but fails under systemd

Usually environment-related. Check:

```bash
systemctl --user status intraday-bot
journalctl --user -u intraday-bot -n 50 --no-pager
```

Common causes:
- Wrong `WorkingDirectory` — must be the bot's repo root, where
  `configs/config.yaml` is reachable
- `.env` not readable by your user (check `ls -la ~/TradingBot/.env`,
  should be `-rw------- <user> <user>`)
- `python3.11` install path mismatch — confirm the venv's Python works
  outside systemd: `~/TradingBot/.venv/bin/python -c "print('ok')"`
- Missing `loginctl enable-linger $USER` — service exits when you log out

### `Permission denied: '.schwabdev/tokens.db'`

Filesystem permissions. The user running the service can't write the
SQLite file. Fix:

```bash
sudo chown -R $USER:$USER ~/TradingBot/.schwabdev/
chmod 700 ~/TradingBot/.schwabdev/
chmod 600 ~/TradingBot/.schwabdev/tokens.db
```

### `cryptography.fernet.InvalidToken` on startup

The `SCHWAB_ENCRYPTION_KEY` in `.env` doesn't match what was used to
write `tokens.db`. Two paths:

1. If you SCP'd `tokens.db` from a machine with a different key:
   copy the source machine's `SCHWAB_ENCRYPTION_KEY` to this machine's
   `.env` and restart.
2. If the key is lost: delete `tokens.db`, redo the OAuth flow with
   whatever key is currently in `.env`.

### journalctl shows old logs only

You're probably looking at journal from before the bot started today.
Try:

```bash
journalctl --user -u intraday-bot --since today -f
journalctl --user -u intraday-bot --since "1 hour ago"
```

The bot's own canonical log is at `.logs/bot_YYYY-MM-DD.log` — that's
always up-to-date regardless of journald state.

### Bot can't write to `.logs/sessions/`

Disk full or the directory isn't writable. Check:

```bash
df -h ~/TradingBot
ls -la ~/TradingBot/.logs/
```

Daily archives are typically <5MB each per-symbol — for a watchlist of
10–20 symbols, expect <100MB per session day. If disk is filling, prune
older archives:

```bash
# Keep only last 30 days of session archives
find ~/TradingBot/.logs/sessions/ -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;
```

Or set `runtime.export_session_archive: false` in `configs/config.yaml`
if you don't need the archives at all.

---

## See also

- `README.md` — main project documentation
- `CHANGELOG.md` — release notes and per-knob behavior changes
- `configs/config.example.yaml` — full annotated config reference
- `intraday_tv_schwab_bot/_strategies/README.md` — strategy plugin
  architecture
- `README_STRATEGY_START_TIMES.md` — entry/management/screener window
  reference
