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

# Then SCP to the server, with the bot there stopped (never over the store
# of a running bot: see "Renewing the Schwab login without a restart"):
scp .schwabdev/tokens.db <user>@<server>:~/TradingBot/.schwabdev/

# Important: the SCHWAB_ENCRYPTION_KEY values must MATCH between machines.
# Copy the same value into ~/TradingBot/.env on the server.
```

To log in from the server itself instead, set
`schwab.open_browser_for_auth: false` and start the bot by hand in a
terminal: schwabdev prints `[Schwabdev] Open to authenticate: <URL>` and
waits for you to paste the address-bar URL the login lands on (open the URL
in any browser; paste within about 30 seconds). The weekly renewal after
that needs neither a desktop nor a restart: see "Renewing the Schwab login
without a restart" below.

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
ExecStart=%h/TradingBot/.venv/bin/python main.py --config configs/config.yaml

# Clean shutdown: once the bot is built, SIGTERM, like Ctrl+C, takes it to
# its shutdown wherever it lands (the start-up reconcile, a cycle, the sleep
# between cycles): it stops the dashboard and the stream, writes the session
# report unless it wrote it at 8pm ET, appends to trades.csv every closed
# trade it holds that the file lacks, then writes the day's archive unless
# it wrote it at 8pm (it starts no export 12s or more after the signal, and
# leaves that day's archive to the next start), then exits 0. The reconcile
# metadata needs no shutdown step: it is saved whenever the tracked
# positions change. Give it 30s, then SIGKILL if it's still hung.
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
- **`TimeoutStopSec=30s`** bounds the shutdown. The bot logs `Interrupted by
  SIGTERM, shutting down.` (the signal's name: `SIGINT` for a Ctrl+C,
  `SIGHUP` for a terminal hangup) once the work the stop interrupted has
  unwound (a cycle's thread pool first finishes its queued work, broker
  reads included) and `Shutdown complete.` when the cleanup is done. Stop
  signals after the first are ignored until then, since one would abandon
  the session report half-written, and the log names any it ignored. A
  stop with no `Shutdown complete.` in the journal means systemd
  SIGKILLed a shutdown that ran past 30s (a hung broker read, a slow
  stream stop): raise the timeout. The archive exports are kept inside
  it: every owed day's report and `trades.csv` append run first, then the
  exports (5-13s each on H:), the days the stopping process ran first,
  then the others, oldest first within each, each started only within
  12s of the stop signal, so the last ends by about 25s. A day whose
  export the shutdown does not start, or whose export fails there, is
  logged (`Shutdown: the <date> archive is not written (...): the next
  start writes it`) and left owed in
  `.logs/sessions/<date>/archive_owed.json`, with the day's skip counts;
  the next start writes it, outside the trading days' 7am-8pm stream
  windows, with `exporter_ran_session: false` and those skip counts. A
  start before 8pm on the day the file names (a restart after a daytime
  stop) leaves it: that process runs the rest of the day and writes the
  day's archive at 8pm as its own (`exporter_ran_session: true`, its own
  skip counts), which removes the file. The file is written before each
  export starts too, so a SIGKILL that cuts an export off leaves its day
  to the next start the same way. Raising the timeout leaves that 12s as
  it is. In a terminal, a second Ctrl+C does nothing while the bot shuts
  down; `kill -9` ends one that hangs.
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
log lines. If it's `failed` (or restarting every 30 s), the journal has the
reason, a `CRITICAL ... Startup refused: ...` line for a config the bot will
not run on, naming the key:
```bash
journalctl --user -u intraday-bot -n 100 --no-pager
```
The day's log, `.logs/bot_$(TZ=America/New_York date +%F).log`, has it too,
with its traceback.

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

# Stop cleanly (writes session report, archives day, unless it wrote them
# at 8pm ET; appends its trades to trades.csv)
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

The bot computes ET session boundaries via `sessions.now_et()`. A clock that's
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

`_ETDailyFileHandler` (in `log_setup.py`) rolls `.logs/bot_YYYY-MM-DD.log` at
midnight ET, every 30s check during emit. **Don't add `logrotate` for
that file** — it'll race with the bot's open handle. journald has its
own rotation governed by `/etc/systemd/journald.conf`.

### Daily session archives

At 8pm ET each trading day the bot writes the day's session report,
appends its closed trades to `.logs/trades.csv` and then writes a
per-day bundle to `.logs/sessions/{YYYY-MM-DD}/`, once a day: a stop
later that day writes neither again, though it appends any trade booked
since. The report is written once; an append that fails (a full disk) is
retried a minute or more later, an archive export after 1 minute, then
waits doubling to 30 minutes, a retry never during a later trading day's
7am-8pm stream window; both at shutdown too, past midnight too. An
archive the shutdown leaves (it starts no export 12s or more after the
stop signal, see `TimeoutStopSec` above) is written by the next start. A
bot started after 8pm never writes that day's report, and writes its
archive only when the day has none (no `manifest.json`: the bot that ran
the day died before 8pm), with `exporter_ran_session: false` in the
manifest, since its bars, account snapshot and skip counts are its own
(the dead bot's skip counts went with it). It still runs the day's
trades.csv append on its first pass (with `auto_exit_after_session`
on, that pass ends the run and its shutdown runs it), a safeguard:
its start-up reconcile books no trade, so it holds none of that
evening. No cron needed. Each archive contains:

- `bars/1m/{SYMBOL}.csv` — the full merged 1m frame with indicators
- `bars/{N}m_from_1m/{SYMBOL}.csv` — that frame resampled to the
  strategy's LTF / HTF minutes when above 1
- `bars/{N}m_schwab_htf/{SYMBOL}.csv` — the stored HTF frame (Schwab's
  N-minute bars) that S/R and the HTF levels are built from
- `trades.csv` — the day's rows of `.logs/trades.csv` that the strategy
  wrote (every process that appended them)
- `bot_YYYY-MM-DD.log` — copy of the daily log
- `events.jsonl` — structured events extracted from the log
- `decisions.csv` — every entry decision as queryable rows
- `account_snapshot.json` — the exporting process's run figures
  (`scope: run`: equity curve, realized PnL, etc.) and the day's (`day`)
- `config_snapshot.yaml` — resolved config (secrets redacted)
- `manifest.json` — strategy + summary stats

Archives written before 2026-10-11 name the resamples `bars/{N}m/` and
the stored HTF frame `bars/htf_{N}m/`. To rename them, and the
manifest keys that name them, stop the bot after pulling the change and,
as the user that owns `.logs`, run
`.venv/bin/python scripts/rename_archive_bars_folders.py .logs/sessions`: it
prints each day's plan and changes nothing. Run it again with `--apply`,
then start the bot. A day it refuses (for example one holding both
names of a series) is left as it was, with the reason printed, and the
exit status is 1.

After a restart on the same day, `trades.csv`, `trades_today` and
`realized_pnl` cover every process of the strategy that appended its
trades, as does `account_snapshot.json`'s `day`, but the manifest's
`session_skip_counts` and `account_snapshot.json`'s run figures are the
last process's alone. A process killed
before it appended (`kill -9`, an OOM kill, systemd's SIGKILL after
`TimeoutStopSec`, a crash before 8pm) leaves its trades out of
`.logs/trades.csv`, the archive's `trades.csv` and its manifest. The
account's day ledger, whose row is written at each exit, still counts
them: `account_snapshot.json`'s `day`, the dashboard's day figures and
the SESSION REPORT's `day_ledger_trades=` hold them. `trades.csv` has no mode column:
a dry-run and a live process of one strategy that share `.logs` on one
day archive each other's rows, under the exporting process's `dry_run`.
Processes sharing `.logs` take turns at `trades.csv` through an advisory
lock on `.logs/trades.csv.lock` (an empty file; leave it in place): an
append that waits more than 5 s for it is retried like any failed append
(a retry inside a later trading day's 7am-8pm stream window tries it once,
without the wait). The bot opens the lock file read-only, so `.logs` on an
NFS mount needs the mount option `local_lock=flock` (NFS's own flock needs
the file open for writing), or no append can take the lock. The bot takes
the lock once at start-up, and logs an ERROR naming the error and that
option when it cannot (it starts all the same).

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

The Schwab refresh token is good for **7 days from the login that issued
it**, and API calls do not extend it: schwabdev renews only the 30-minute
access token by itself. A new refresh token takes a new login. Once less
than 3630 s (60.5 minutes) of the refresh token is left, schwabdev starts
its login flow inside whichever API call checks the token next: it prints
`[Schwabdev] Open to authenticate: <URL>`, opens a browser where it can, and
waits on `input()` for the redirected URL.

- **On a terminal** (the bot started by hand, or in tmux), the engine waits
  at that prompt, mid-session if that is when the hour starts, and every
  other caller waits behind it. Paste the URL within 30 seconds of logging
  in and the bot carries on with a new 7-day token. While it waits there
  it holds `tokens.db` locked, so `scripts/renew_schwab_token.py` refuses
  to run: paste at the bot's prompt.
- **Under systemd** there is no stdin, so `input()` raises `EOFError`, and
  schwabdev logs `WARNING Schwabdev The refresh token is expiring soon
  (<60min)!` and `ERROR Schwabdev [Schwabdev] Could not update refresh
  token (EOF when reading a line)` on every API call (from 14:07:30 on
  2026-09-25). That check comes before the access token's, so schwabdev
  stops renewing the access token too: every call fails with `401
  Unauthorized` once the current one lapses (at most 30 minutes; at
  14:08:26 that day), until the login is renewed with
  `scripts/renew_schwab_token.py` (below): the bot's next call takes the
  new tokens, no restart. Meanwhile the engine can neither read a quote
  nor send an order; only orders already resting at Schwab protect a
  position.

The bot logs the token's window at start-up and on the first pass of each
trading day, from `tokens.db`, read-only:

```
INFO Schwab refresh token issued 2026-10-06 14:48:37 EDT, expires 2026-10-13 14:48:37 EDT; schwabdev asks for a new login from 2026-10-13 13:48:07 EDT, after the session after the coming one ends (2026-10-09 16:00:00 EDT).
```

It is a WARNING when that login falls before the session after the coming
one ends: `..., before the coming session ends (...): log in again before
then. ...`, or `..., before the session after the coming one ends (...):
...` when the login falls after the coming session ends. The coming session
is today's while it lasts, else the next trading day's, and a session ends
at the regular close (13:00 on an early-close day) or the strategy's last
window, whichever is later; weekends and exchange holidays hold none. The
WARNING comes a trading day ahead because a bot that exits after each
session (`auto_exit_after_session`, on in all but one shipped preset) logs
this once a day, at start-up, and the next day's start-up may meet
schwabdev's prompt inside the client's own start, before the bot logs
anything: the day before is the last warning you can act on outside a
session. The line above was logged on Thu 2026-10-08; the same token logs
INFO through Fri 10-09, then `before the session after the coming one ends
(2026-10-13 16:00:00 EDT)` on Mon 10-12 and `before the coming session ends
(2026-10-13 16:00:00 EDT)` on Tue 10-13. Once the login time has passed (a
bot started after it, or after the token expired), the WARNING says so:
`..., expires (or expired) ...; schwabdev has asked for a new login since
...: log in again now (or paste at the bot's own prompt if it waits at
one). ...`. Both WARNINGs end with the command that renews the login from
a terminal while the bot runs (below): to the bot's working folder, then
the script by its full path, with the `--config` (and `--env`, when it was
given one) the bot was started with:

```
... To log in again from a terminal while the bot runs (no restart needed): cd /home/<user>/TradingBot && .venv/bin/python /home/<user>/TradingBot/scripts/renew_schwab_token.py --config configs/config.yaml
```

(An installed package, with no scripts folder beside it, says to run the
script from a source checkout of that version instead.)

A `tokens.db` it cannot read is a WARNING naming the error's type (`Schwab
refresh token: could not read schwabdev's token store ...`). Neither
blocks entries. Watch for them in the day's log, or in the journal:

```bash
grep 'Schwab refresh token' ~/TradingBot/.logs/bot_$(TZ=America/New_York date +%F).log
journalctl --user -u intraday-bot --since today | grep 'Schwab refresh token'
```

Not `journalctl -p warning`: the bot writes its lines to stdout as plain
text, so the journal files every one of them at priority `info`, its
WARNINGs included, and a priority filter drops them all.

### Renewing the Schwab login without a restart

`scripts/renew_schwab_token.py` renews the login beside the running bot,
from any terminal on the machine the bot runs on (an SSH session will do);
the bot keeps running. Run it as the user that runs the bot (from the
bot's folder, for the case where the bot is stopped). It needs no flags:

```bash
cd ~/TradingBot
.venv/bin/python scripts/renew_schwab_token.py --status   # the checks and the window only
.venv/bin/python scripts/renew_schwab_token.py            # renew
```

**Which settings: the running bot's.** It finds the bot of its checkout
in /proc: a process of your user running the checkout's `main.py` (from
any working folder), or the `intraday-tv-schwab-bot` console script
working in the checkout. It takes that bot's `--config` and `--env`, read
as the bot read them, and reads them, the `.env` and a relative
`schwab.tokens_db` in the bot's working folder, as the bot did; it says so
first: `The bot of /home/<user>/TradingBot running: pid 1234, working in
/home/<user>/TradingBot (--config configs/config.yaml, no --env).` The app
key, the app secret and the key are the bot's too: the environment the
bot was started with (a systemd unit's `Environment=`, say) wins over its
`.env`, as the bot's loader let it, and your shell's own `SCHWAB_*`
variables are never used while a bot runs (a shell exporting another app's
key would otherwise log in as that app, whose tokens the bot cannot
refresh). The summary says where each came from, never the value; a bot
whose environment cannot be read is refused.

**What the bot loaded.** /proc keeps the environment the bot started with,
but not its config and `.env`: the script reads those as they are now. So
it refuses when the bot's config or its `.env` (the ones it finds for the
bot) changed after the bot started, naming the file (`Refused:
/home/<user>/TradingBot/.env changed at ..., after the running bot, pid
1234, started (...)`): a `.env` given another app for the next restart, or
a preset a pull changed, would otherwise renew an app or a store the bot
does not run with. Restart the bot (it then reads the file as it is; past
the store's login time a starting bot meets schwabdev's prompt, so stop
it, renew with the bot's `--config` while it is stopped, then start it). A
file put back with its old modification time (`mv`, `cp -p`, `rsync -a`)
still counts as changed: its status-change time, which nothing sets back,
is new, and so is that of a file whose mode or owner changed. And the
store it renews must be the file the running bot holds open (the same
device and inode, from `/proc/<pid>/fd`; schwabdev keeps the store open):
a store replaced by a copy or a rename while the bot runs, or one found
elsewhere (a preset changed with its time kept, another `--config`, a `~/`
path read with another home folder), is refused, naming that store and the
sqlite files the bot holds open (`... (deleted)` for a replaced one).

`--config` and `--env` (relative to the folder you run it from) are
checked against the running bot, each config loaded as the bot loads it
(the bot's environment over that config's `.env`): one that names another
token store (both are named), or gives other credentials (an `--env` with
another app's key and secret, say), is refused: the renewal would not
reach the bot. With no bot running they are used, read in the folder
you run it from (with your shell's `SCHWAB_*` over the `.env`, as a bot
started there would take them), and the summary says the bot reads the
store at its next start. With no bot running and no `--config` it stops,
lists the presets in `configs/` and asks for `--config` (and `--env`, if
the bot was given one); it never falls back to a default config. Several
bots running are refused: it renews one bot's login. A bot of another
user is named, with that user to run it as. A bot of the checkout whose
command line the bot's parser no longer reads (a `--strategy` the pulled
code dropped) is still the running bot (its environment, the store it
holds open), but its `--config` cannot be told: refused, saying why,
unless `--config` is given, which is used only if it names the store that
bot holds open. The bot's day-ahead WARNING ends with the exact command,
flags included.

**The checks, before the login.** On a copy of `tokens.db` (in memory) it
proves the settings fit the store: the configured key must read the stored
tokens. Refused, naming where the key came from: tokens stored in clear
while this run has a key (the bot runs without one; with no bot running,
a `SCHWAB_ENCRYPTION_KEY` your shell exports wins over the `.env`: unset
it), encrypted tokens and no key, and another key. A
`tokens.db` (or its folder) your user cannot write is refused too: Schwab
takes the code before the store is written, so a write that fails would
spend the login. So are a store that is missing (with no bot running: a
first login is the bot's own first start) or locked (the bot writing its
tokens: try again; or a bot waiting at schwabdev's own prompt: paste there
instead). Then it shows what it will renew and asks:

```
The bot of /home/<user>/TradingBot running: pid 1234, working in /home/<user>/TradingBot (--config configs/config.yaml, no --env).
  config      /home/<user>/TradingBot/configs/config.yaml (the --config of the running bot, pid 1234)
  .env        /home/<user>/TradingBot/.env (found as the bot finds it: the running bot has no --env)
  app key     SCHWAB_APP_KEY in /home/<user>/TradingBot/.env
  app secret  SCHWAB_APP_SECRET in /home/<user>/TradingBot/.env
  key         SCHWAB_ENCRYPTION_KEY in /home/<user>/TradingBot/.env
  tokens      encrypted (this run's key reads them)
  bot         pid 1234, working in /home/<user>/TradingBot: it takes the new tokens from the store at its next Schwab call
The Schwab refresh token in /home/<user>/TradingBot/.schwabdev/tokens.db:
  issued   2026-10-06 14:48:37 EDT
  expires  2026-10-13 14:48:37 EDT
  schwabdev asks for a new login from 2026-10-13 13:48:07 EDT
Renew it? [y/N]:
```

**The login.** Confirmed, it starts schwabdev's client on `tokens.db`
first (after an evening's idle, that start refreshes the lapsed access
token, before the URL is shown, so none of the code's life goes to it),
prints Schwab's login URL for your app key and callback URL, and waits:

1. Open the URL in a browser, on any machine, and log in to Schwab.
2. The login lands on your callback URL (`https://127.0.0.1/?code=...`), a
   page that does not load. Copy the whole address-bar URL and paste it at
   the prompt within about 30 seconds: the code in it expires.

It then trades the code for new tokens and prints the new window:

```
Renewed.
The Schwab refresh token in /home/<user>/TradingBot/.schwabdev/tokens.db:
  issued   2026-10-13 00:10:00 EDT
  expires  2026-10-20 00:10:00 EDT
  schwabdev asks for a new login from 2026-10-19 23:09:30 EDT
The running bot, pid 1234, takes the new tokens from the store at its next Schwab call, no restart (its log: "Schwab tokens taken from the token store").
Renew before then: by the close of the last session ahead of it (2026-10-19 16:00:00 EDT), or after that close while the bot idles.
```

A bot that started before `intraday_tv_schwab_bot/schwab_api.py` last
changed (one running from before a pull) may run the code before this,
which takes a renewal only at its next access-token refresh. The summary
and the last lines say so for it: `pid 1234, working in
/home/<user>/TradingBot, started before
/home/<user>/TradingBot/intraday_tv_schwab_bot/schwab_api.py changed: it
takes the new tokens from the store at its next access-token refresh
(within 30 minutes during a session; after an idle stretch, at its first
call)`.

Run it in a terminal: a here-document (`python - <<'PY' ... PY`) or a pipe
leaves its prompts nothing to read, so it refuses to start. A pasted URL
that holds no code is refused before the code is sent (the client's start
before it may have refreshed a lapsed access token; the refresh token is as
it was). A failed exchange is
reported with schwabdev's reason; a code lasts about 30 seconds, so run it
again and paste the new URL at once. If schwabdev's client cannot start
because another process held `tokens.db`'s lock for more than 5 seconds
(the bot writing its tokens), it says so before the URL is shown, and no
code was sent: run it again. It prints neither the app secret, nor a
token, nor the pasted URL, and warns when `tokens.db` can be read or
written by its group or others (fix: `chmod 600 .schwabdev/tokens.db`).
Exit status: 0 renewed (with `--status`: the summary printed), 1 not
renewed, 2 refused before the login (the login is as it was).

**Where.** On the machine the bot runs on, with `tokens.db` on that
machine's own disk. From another host over a shared folder (the checkout's
SMB share, WSL, NFS) two things go wrong. The issue times are stamped by
that host's clock, and the bot trusts them: a clock 10 minutes ahead makes
the bot send a dead access token for 10 minutes, and ask for its next login
10 minutes after its refresh token died. And sqlite's locking is not
reliable over SMB or NFS, so the renewal and the bot can write over each
other.

Never replace `tokens.db` by a copy or a rename while the bot runs
(`cp` or `rsync` over it, `mv`, an editor's save): the bot keeps the file
it opened. A renewed copy renamed over it is taken at the bot's next call,
then undone at its next refresh, when schwabdev refreshes from the file it
still holds (unlinked) with the old refresh token; the path then shows the
renewal while the bot runs on the old login until it expires (the script
refuses such a store: the bot does not hold it open). A copy written over
the open file bypasses sqlite's locks. The script writes the store in
place, through sqlite.

**What a held lock costs.** While the script trades the code (usually under
a second; schwabdev waits up to 30 s for Schwab's answer) it holds
`tokens.db`'s lock, and so does the client's start while it refreshes a
lapsed access token. A bot that reaches its own access-token refresh
meanwhile waits for that lock, and every Schwab call of the bot waits
behind it: no quote and no order for that long, 30 s at most. Past that its
refresh fails and the call carries its own token (one 401 at worst, if that
token lapsed meanwhile). After the close, while the bot idles, it costs
nothing.

**When.** Once a week, before the login time the bot's `Schwab refresh
token` line names (its WARNING, a trading day ahead, ends with the
command). The bot makes no Schwab call between sessions, so renew by the
close of the last session before that time, or in the idle hours after it,
as the script's last line says: a renewal during a session reaches the bot
at its next call, but risks the lock's cost above. A renewal in the evening
puts the next login time in an evening too, a week on.

**What the bot logs after.** At its next Schwab call it takes the new
tokens from `tokens.db`: `INFO intraday_tv_schwab_bot.schwab_api Schwab
tokens taken from the token store /home/<user>/TradingBot/.schwabdev/tokens.db,
which another process wrote: access token issued 2026-10-13 00:10:00 EDT,
refresh token issued 2026-10-13 00:10:00 EDT`. After an evening renewal
that access token has lapsed by the next session (it lives 30 minutes), so
schwabdev refreshes it in the same check, before a request carries it:
`INFO Schwabdev Access token updated at ...`. A store the bot cannot read at
that call (another process writing it) leaves it its tokens, with one
`WARNING ... Schwab token store ... changed but could not be read (...)`,
and the next call tries again. Tokens written under a key the bot does not
have are an ERROR, `Schwab token store ... holds tokens another process
wrote that this bot's key (schwab.encryption) does not decrypt (...)`. Only
another writer can store them (the script refuses a key that does not
read the store, and takes the bot's own), and the bot keeps its own tokens
only until its next refresh point: there schwabdev's own reload takes that
write's issue times but not its tokens, so the bot sends its own access
token past its expiry (401s), then refreshes with its old refresh token and
writes it back, as it keeps its tokens, under that write's issue time: a
window `--status` and the WARNING show but its refresh token does not
have. Until that write-back the script refuses the store (its tokens do
not read with the bot's key): renew with it, no flags, once it no longer
refuses (within 30 minutes), or restart the bot with the key that wrote
the store. The next trading day's `Schwab refresh token issued ...` line
shows the renewal's time; `--status` shows it at once.

**The deadline.** The refresh token expires 7 days after the login that
issued it, and schwabdev asks for a new login once less than 3630 s of it
is left. Past that time, a bot under systemd fails every call (above) until
the login is renewed; run the script then too, and the bot recovers at its
next call, no restart. A bot stopped for more than 7 days needs the
renewal before it starts again: the script works with the bot stopped
too, given the bot's `--config` (and `--env`), since there is no running
bot to find them on; run it from the bot's working folder (where a
relative `tokens_db` and the `.env` are read), with the shell
environment the bot starts with.

### TradingView session expiry

The TV `sessionid` cookie expires periodically (usually weeks to months).
When it does, the screener stops returning candidates and the bot logs
warnings. With `Restart=on-failure`, the bot will keep retrying — but it
won't actually fix itself. Watch the bot's warnings and errors (filtered
on the level in the line: the journal files all of the bot's lines at
priority `info`, so `-p warning` would show none of them):

```bash
journalctl --user -u intraday-bot -f | grep -E ' (WARNING|ERROR|CRITICAL) '
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

The clean-shutdown path runs first (writes session report + archive
unless it wrote them at 8pm ET, and appends its trades to trades.csv),
then the new code starts. In live mode the open positions are restored
from the broker at the start, their levels from the reconcile metadata
SQLite store. A dry run's paper positions are not restored: each one open
at the restart is dropped, logged when the start-up reconcile ends
(`Day ledger: <symbol> <side> <qty>, open in the <day> row, was not
restored: its move up to the restart, <amount>, stays in the day's P&L,
never realized and in no trade`), and that move stays in the day's P&L.
Restart a dry run flat (top_tier is flat from 15:50).

---

## Quicker alternatives (not recommended for production)

| Approach | Use case | Why not for production |
|---|---|---|
| `tmux new -d -s bot 'cd ~/TradingBot && .venv/bin/python main.py --config configs/config.yaml'` | Quick one-off testing | No auto-restart on crash; doesn't survive reboot |
| `nohup ... &` + `disown` | Throwaway run | Loses stderr, no auto-restart; `nohup` keeps the logout's hangup (SIGHUP) ignored, so stop it with `kill <pid>` |
| Docker / Podman | Already a container shop | Extra layer; bot already isolated via venv |
| `supervisord` | Older systems without systemd | systemd is everywhere on modern Linux |

For dev and one-off testing, `tmux` is fine. `Ctrl+B` then `x` (kill-pane)
or `Ctrl+B` then `&` (kill-window, which closes every pane in the window)
asks `y/n` first; on `y` the bot gets the hangup (SIGHUP) and still runs its
clean shutdown (session report, archive) behind it, as it does when an SSH
session running it in the foreground drops. The one-liner above makes a
session with one window and one pane, so either key closes just the bot.
For anything you want to leave running across days, use the systemd unit
above.

---

## Troubleshooting

### "Access denied" message during first auth + can't Ctrl+C

schwabdev's `webbrowser.open()` spawns a subprocess (xdg-open or browser
binary) that steals the foreground process group's signals. The Python
parent is sitting at `input()` waiting for a paste — but Ctrl+C goes to
the subprocess, not Python. Symptom: terminal wedged, Ctrl+C does
nothing.

**Recovery:** kill it from another shell. However this guide starts the
bot (by hand, the systemd unit, `start_trading_bot.sh`, which `exec`s it,
or the tmux one-liner), its command line is `<python> main.py --config
...`, where `<python>` is `python` or a path to the venv's; the package
name is not on it. List the bots, then kill the wedged one:
```bash
pgrep -af '^[^ ]*python[^ ]* main\.py --config'
kill -9 <pid>
```
The pattern starts at the interpreter, so it leaves out processes that only
carry the command as text: the tmux server the one-liner started, and a
shell that wraps it (`sh -c '... main.py --config ...'`). A bot the systemd
unit runs is listed too; `ps -o pid,tty,args -p <pid>` tells them apart
(the unit's has no terminal: `?`).

**Don't fight this — sidestep it.** Auth on a machine that has a
browser, SCP the `tokens.db` over with the bot stopped (see "First-time
Schwab OAuth" above), or set `schwab.open_browser_for_auth: false` so no browser is
spawned. The encryption key in `.env` must match between machines. The
weekly renewal, `scripts/renew_schwab_token.py`, never opens a browser.

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
