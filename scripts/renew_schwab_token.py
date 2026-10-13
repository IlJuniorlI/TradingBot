# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""Renew the Schwab login from a terminal while the bot runs.

    .venv/bin/python scripts/renew_schwab_token.py [--config PATH] [--env PATH]
    .venv/bin/python scripts/renew_schwab_token.py --status [--config PATH] [--env PATH]

schwabdev 4.0.0 keeps a 7-day refresh token in the token store
(``schwab.tokens_db``), and only a login in a browser issues a new one. Once
less than 3630 s of it is left, the bot's next Schwab call starts
schwabdev's own login prompt, which a bot without a terminal cannot answer
(the bot's WARNING says when, a trading day ahead, and ends with this
script's command line for that bot). This script makes that login beside
the running bot, which takes the new tokens from the store at its next
Schwab call, no restart (``PooledSchwabClient._take_store_write``).

Which settings: the running bot's. On Linux it finds the bot of this
checkout in /proc: a process of this user running the checkout's
``main.py`` (from any working directory), or the
``intraday-tv-schwab-bot`` console script working in the checkout. It
takes the bot's ``--config`` and ``--env``, read as the bot read them
(``cli.argument_parser``), and reads them, the .env and a relative
``schwab.tokens_db`` in the bot's working directory, as the bot did. The
app key, the app secret and the key (``schwab.encryption``) are the bot's
too: the environment the bot was started with (/proc/<pid>/environ:
SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_ENCRYPTION_KEY) wins over its
.env, as the bot's loader let it; this shell's own SCHWAB_* variables are
never used while a bot runs. A bot whose environment cannot be read is
refused. The summary says where each came from, never the values.

What the bot loaded: /proc keeps the environment the bot started with,
not its config and .env, which this run reads as they are now. A config
or .env (the ones this run reads for the bot) changed after the bot
started (its start: /proc/<pid>/stat) is refused, naming the file:
restart the bot, or put the file back. And the token store renewed must be
the file the bot holds open (schwabdev keeps it open: /proc/<pid>/fd, the
same device and inode), else refused, naming both: a store replaced by a
copy or a rename while the bot runs, or one found elsewhere (a config
changed with its time kept, another ``--config``, another home folder).

``--config`` and ``--env`` (relative to the folder this is run from) are
checked against a bot found, each config loaded as the bot loads it (the
bot's environment over that config's .env): refused when they name
another token store (both are named) or give other credentials than the
bot runs with. With no bot running they are used (relative paths in them
read in the folder this is run from, and the credentials in this shell's
environment over the .env), and the summary says the bot reads the store
at its next start; with no bot running and no ``--config`` it stops and
lists the presets: it never falls back to a default config. Several bots
running are refused: it renews one bot's login. A bot of this checkout
whose command line the bot's parser does not read (a ``--strategy`` the
pulled code dropped) is still the bot (its environment, the store it
holds open), but its ``--config`` cannot be told: refused unless
``--config`` is given, which is used only if it names that store.

Before the login it proves the settings fit the token store. On a copy of
the store (sqlite's backup, in memory), the configured key must read the
stored tokens: tokens in clear with a key configured, encrypted ones with
none, and another key are refused. The user running it must be able to
write the store and its folder: Schwab takes the code before the store is
written, so a write that fails would spend the login. It then shows what it
will renew (the config, the .env and how each was found, where the app
key, the app secret and the key came from, the store's path, the refresh
token's window in ET, encrypted or not, the bot that takes it and when: at
its next Schwab call, or, for a bot that started before the code that
takes it (intraday_tv_schwab_bot/schwab_api.py) last changed, at its next
access-token refresh) and asks to confirm.

The renewal, confirmed:

1. builds schwabdev's client on the store. Its start refreshes an access
   token that has lapsed (an evening renewal's usual case) before the login
   URL is shown, so none of the code's life goes to it; the client is
   handed the pasted URL only once there is one.
2. prints the login URL for the config's app key and callback URL and waits
   for the address-bar URL the login lands on (a page that does not load).
   Paste it within about 30 seconds: the code expires.
3. forces the renewal (``update_tokens(force_refresh_token=True)``):
   schwabdev locks the store, trades the code for new tokens and writes
   them, in place. A bot call that needs the store meanwhile waits for the
   lock (usually under a second, at most schwabdev's 30 s timeout). Never
   replace the store by a copy or a rename while the bot runs instead: the
   bot keeps the file it opened (README_LINUX_DEPLOY.md).

Refused before the code is sent: a stdin that is not a terminal (a
here-document, ``python - <<'PY'``, leaves the prompts nothing to read), a
store that is missing (with no bot running: a first login is the bot's
own first start) or locked (the bot writing its tokens: try again; a bot
waiting at schwabdev's own prompt: paste there instead), and a pasted URL
that is not on the callback URL or holds no code (the client's start,
before the paste, may have refreshed a lapsed access token: the refresh
token is as it was).

``--status`` makes the same checks and prints the same summary and when to
renew next: no client, no request.

Neither prints the app secret, a token or the pasted URL. Both warn when the
store can be read or written by its group or others.

Exit status: 0 renewed (with ``--status``: the summary printed); 1 not
renewed (schwabdev's client could not start, or the exchange failed); 2
refused before the login: no code was sent and the login is as it was.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sqlite3
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
# The script runs from a checkout (python scripts/renew_schwab_token.py),
# whose package sits beside scripts/, not on the script's own path.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import schwabdev  # noqa: E402
from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from intraday_tv_schwab_bot import schwab_api, sessions  # noqa: E402
from intraday_tv_schwab_bot.cli import argument_parser, default_config_path  # noqa: E402
from intraday_tv_schwab_bot.config import BotConfig, dotenv_path, load_config  # noqa: E402
from intraday_tv_schwab_bot.engine import session_ends_after  # noqa: E402
from intraday_tv_schwab_bot.schwab_api import (  # noqa: E402
    RefreshTokenWindow,
    read_refresh_token_window,
    stored_time,
    token_store_path,
)

# Where the running bot is looked for (Linux), and how its command line
# names it: a Python interpreter running main.py, or the console script
# pyproject.toml declares (its shebang line runs it as the interpreter, then
# the script's path).
PROC = Path("/proc")
CONSOLE_SCRIPT = "intraday-tv-schwab-bot"
BOT_SCRIPTS = ("main.py", CONSOLE_SCRIPT)
_PYTHON = re.compile(r"python[0-9.]*")
# The settings the bot's loader takes from the process environment over the
# .env (``config.load_config``; a real value in the config's schwab section
# wins over both): the config's field, what the summary calls it, and the
# variable.
CREDENTIALS = (("app_key", "app key", "SCHWAB_APP_KEY"), ("app_secret", "app secret", "SCHWAB_APP_SECRET"),
               ("encryption", "key", "SCHWAB_ENCRYPTION_KEY"))
THIS_SHELL = "this shell's environment"
STAMP = "%Y-%m-%d %H:%M:%S %Z"
# The code that has a running bot take a renewal at its next Schwab call
# (``PooledSchwabClient._take_store_write``): a bot that started before it
# last changed may run the code before it, which takes a renewal only at
# its next access-token refresh (schwabdev's own reload of the store).
TAKER = Path(schwab_api.__file__)
# The first bytes of an sqlite database file (a -wal, -journal or -shm file
# starts otherwise): how the token stores a bot holds open are told apart
# from its other open files, to name them.
SQLITE_HEADER = b"SQLite format 3\x00"
# schwabdev 4.0.0's login URL (``Tokens._update_refresh_token``), its marker
# for an encrypted token (``tokens._ENC_PREFIX``) and the shortest key it
# uses: one of 16 characters or fewer is taken for none (``Tokens.__init__``).
AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
ENCRYPTED_PREFIX = "enc:"
SHORTEST_KEY = 17
CONFIRM_PROMPT = "Renew it? [y/N]: "
PASTE_PROMPT = "Address-bar URL: "
NOT_A_TERMINAL = (
    "stdin is not a terminal, so the prompts would have nothing to read: a here-document (python - <<'PY' ...) "
    "or a pipe leaves them at its end, and they fail at once (EOFError). Run this script from a terminal: "
    ".venv/bin/python scripts/renew_schwab_token.py")
UNCHANGED = "The login is as it was."


class Refused(Exception):
    """Why the script stops before the login (exit status 2)."""


@dataclass(frozen=True)
class BotProcess:
    """A bot of this checkout that runs: its pid, its working directory,
    when it started (ns since the epoch: ``started_at``), whether that was
    before ``TAKER`` last changed, and its ``--config``, ``--env`` and
    ``--strategy`` as it was given them (None: not given), or why the bot's
    parser does not read its command line (``unparsed``; its ``--config``
    and ``--env`` are then read without the parser, ``_given``, and its
    ``--strategy`` is None: not known)."""
    pid: int
    cwd: Path
    started: int
    before_taker: bool
    config: str | None
    env: str | None
    strategy: str | None
    unparsed: str | None = None

    def flags(self) -> str:
        if self.unparsed is not None:
            return f"a command line the bot's parser does not read: {self.unparsed}"
        config = f"--config {self.config}" if self.config is not None else "no --config"
        return f"{config}, --env {self.env}" if self.env is not None else f"{config}, no --env"


@dataclass(frozen=True)
class Found:
    """What was found running: the bot (None: none, or not looked for), and
    what to say about it first."""
    bot: BotProcess | None
    seen: str
    looked: bool


@dataclass(frozen=True)
class Settings:
    """The config and .env this run reads (absolute; ``env`` None: found
    as the bot finds it), how each was found, and the running bot's own
    (``bot_config``, ``bot_env``) when one was found: the same unless
    ``--config`` or ``--env`` was given (``given`` names which); None for
    a bot whose command line its parser does not read (its own cannot be
    told: the ``--config`` given stands for it)."""
    config: Path
    env: Path | None
    config_found: str
    env_found: str
    bot: BotProcess | None
    bot_config: Path | None = None
    bot_env: Path | None = None
    given: str = ""


@dataclass(frozen=True)
class Loaded:
    """The config this run renews with, and where its app key, app secret
    and key came from (by field: never the value)."""
    config: BotConfig
    sources: dict[str, str]


@dataclass(frozen=True)
class Store:
    """The token store this run renews, its refresh token's window, and how
    its tokens are kept (said in the summary)."""
    path: Path
    window: RefreshTokenWindow
    kept: str


def _et(moment: datetime) -> str:
    return f"{moment.astimezone(sessions.EXCHANGE_TZ):{STAMP}}"


def _et_ns(moment: int) -> str:
    """A time in ns since the epoch, in ET to the second."""
    return _et(datetime.fromtimestamp(moment // 1_000_000_000, timezone.utc))


def _absolute(value: str | None) -> Path | None:
    """A path given on this script's command line, against the folder it
    was run from."""
    return Path(os.path.expanduser(value)).absolute() if value is not None else None


def _in_folder(folder: Path, value: str) -> Path:
    """A path a bot was given, against its working directory."""
    return folder / os.path.expanduser(value)


def _owner(path: Path) -> str:
    try:
        return path.owner()
    except (KeyError, OSError) as exc:
        return f"another user ({type(exc).__name__})"


# ---- the running bot ------------------------------------------------------

def _script_index(argv: list[str]) -> int | None:
    """Where a Python command line names its script, past the interpreter's
    own options; None when it runs none (``-c``, ``-m``, stdin)."""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return index + 1 if index + 1 < len(argv) else None
        if arg == "-":
            return None
        if not arg.startswith("-"):
            return index
        if arg.startswith("--"):
            index += 2 if arg == "--check-hash-based-pycs" else 1
            continue
        for position, flag in enumerate(arg[1:], start=2):
            if flag in "cm":
                return None
            if flag in "WX":
                # Its value is the rest of this argument, or the next one.
                index += 1 if position < len(arg) else 2
                break
        else:
            index += 1
    return None


def _bot_script(argv: list[str]) -> int | None:
    """Where ``argv`` names the bot's script (``BOT_SCRIPTS``) after a
    Python interpreter, else None."""
    if len(argv) < 2 or not _PYTHON.fullmatch(Path(argv[0]).name):
        return None
    index = _script_index(argv)
    if index is None or Path(argv[index]).name not in BOT_SCRIPTS:
        return None
    return index


def bot_arguments(argv: list[str], cwd: Path, root: Path) -> list[str] | None:
    """The bot's own arguments, when ``argv`` (a command line run in
    ``cwd``) runs the bot of the checkout ``root``: its main.py, from any
    working directory, or the console script working in ``root`` (it names
    no checkout); else None."""
    index = _bot_script(argv)
    if index is None:
        return None
    script = Path(argv[index])
    if script.name == "main.py":
        if (cwd / script).resolve() != root / "main.py":
            return None
    elif cwd != root:
        return None
    return argv[index + 1:]


def _maybe_from(argv: list[str], root: Path) -> bool:
    """Whether ``argv``, run in a folder that cannot be read, may run the
    bot of the checkout ``root``: the console script, or a main.py that is
    ``root``'s or given relative to that folder."""
    index = _bot_script(argv)
    if index is None:
        return False
    script = Path(argv[index])
    return script.name != "main.py" or not script.is_absolute() or script.resolve() == root / "main.py"


def _command_line(entry: Path) -> list[str]:
    parts = (entry / "cmdline").read_bytes().split(b"\0")
    if parts[-1] == b"":
        parts.pop()
    return [os.fsdecode(part) for part in parts]


def _process_cwd(entry: Path) -> Path:
    return Path(os.readlink(entry / "cwd"))


def started_at(entry: Path) -> int:
    """When the process ``entry`` (/proc/<pid>) started, in ns since the
    epoch: its start in clock ticks after boot (``stat``'s field 22, read
    past the command name, which may hold spaces and parentheses) on the
    boot clock read now (CLOCK_BOOTTIME, the one that field counts on). Cut
    to its tick (10 ms at 100 a second), it errs early: a file written in
    the tick before the start counts as changed after it. ``OSError`` when
    the process ended meanwhile; ``ValueError`` when the field does not
    read."""
    fields = (entry / "stat").read_text().rpartition(")")[2].split()
    if len(fields) < 20:
        raise ValueError(f"{entry / 'stat'} holds {len(fields)} fields after the command name, not 20 or more")
    ticks = int(fields[19])
    boot = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    return boot + ticks * 1_000_000_000 // os.sysconf("SC_CLK_TCK")


def _taker_changed() -> int:
    """When ``TAKER`` last changed (ns since the epoch), else ``Refused``."""
    try:
        return TAKER.stat().st_mtime_ns
    except OSError as exc:
        raise Refused(f"{TAKER} could not be looked at ({type(exc).__name__}: {exc}): whether the running bot runs "
                      f"the code that takes a renewal at its next call cannot be told. {UNCHANGED}") from exc


def _parsed(parser: argparse.ArgumentParser, args: list[str]) -> argparse.Namespace | str:
    """``args`` read by the bot's parser, or why it refuses them (argparse
    prints why and exits: its lines are caught here, not printed)."""
    said = io.StringIO()
    try:
        with redirect_stderr(said), redirect_stdout(said):
            options, _rest = parser.parse_known_args(args)
    except SystemExit as exc:
        lines = said.getvalue().strip().splitlines()
        why = lines[-1].partition(": error: ")[2] if lines else ""
        return why or f"argparse exit status {exc.code}"
    return options


def _given(args: list[str], flag: str) -> str | None:
    """The value of ``flag`` in a command line the bot's parser refuses
    (``flag VALUE`` or ``flag=VALUE``, the last given, as argparse takes
    it), read without the parser; None when not given."""
    value = None
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith(flag + "="):
            value = arg[len(flag) + 1:]
    return value


def find_bots(proc: Path, root: Path) -> tuple[list[BotProcess], list[str]]:
    """The bots of the checkout ``root`` that run (``bot_arguments``), read
    from ``proc`` (/proc: each process's command line, working directory
    and start), with their ``--config``, ``--env`` and ``--strategy`` read
    by the bot's own parser, or why it does not read them (still a bot of
    the checkout); and the processes that run a bot but whose working
    directory this user may not read (another user's), said with their
    owner. ``Refused`` when a bot's start does not read."""
    bots: list[BotProcess] = []
    unread: list[str] = []
    taker_changed: int | None = None
    parser = argument_parser()
    parser.set_defaults(config=None)
    for entry in sorted((e for e in proc.iterdir() if e.name.isdigit()), key=lambda e: int(e.name)):
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            argv = _command_line(entry)
        except OSError:
            continue    # it ended meanwhile
        try:
            cwd = _process_cwd(entry)
        except PermissionError:
            if _maybe_from(argv, root):
                owner = _owner(entry)
                unread.append(f"pid {pid} runs a bot as {owner}, from a folder this user cannot read: if it is the "
                              f"bot of {root}, run this script as {owner}")
            continue
        except OSError:
            continue    # it ended meanwhile
        args = bot_arguments(argv, cwd, root)
        if args is None:
            continue
        try:
            started = started_at(entry)
        except (FileNotFoundError, ProcessLookupError):
            continue    # it ended meanwhile
        except (OSError, ValueError) as exc:
            raise Refused(f"when pid {pid}, the bot of {root}, started could not be read ({type(exc).__name__}: "
                          f"{exc}): whether its config or .env changed since cannot be told. {UNCHANGED}") from exc
        if taker_changed is None:
            taker_changed = _taker_changed()
        options = _parsed(parser, args)
        if isinstance(options, str):
            bots.append(BotProcess(pid, cwd, started, started < taker_changed, _given(args, "--config"),
                                   _given(args, "--env"), None, unparsed=options))
            continue
        bots.append(BotProcess(pid, cwd, started, started < taker_changed, options.config, options.env,
                               options.strategy))
    return bots, unread


def _bots_seen(bots: list[BotProcess], unread: list[str], root: Path) -> str:
    if len(bots) == 1:
        seen = f"The bot of {root} running: pid {bots[0].pid}, working in {bots[0].cwd} ({bots[0].flags()})."
    elif bots:
        listed = "; ".join(f"pid {bot.pid}, working in {bot.cwd} ({bot.flags()})" for bot in bots)
        seen = f"{len(bots)} bots of {root} are running: {listed}."
    else:
        seen = (f"No bot of {root} is running (a python process of this user running {root / 'main.py'}, or the "
                f"{CONSOLE_SCRIPT} console script working in {root}).")
    return " ".join([seen, *(f"{line}." for line in unread)])


def presets(root: Path) -> str:
    names = sorted(f"configs/{path.name}" for path in (root / "configs").glob("*.yaml"))
    return ", ".join(names) if names else "none"


def look_for_the_bot(config_given: bool, root: Path, proc: Path) -> Found:
    """The bot of the checkout ``root`` that runs (``find_bots``).
    ``Refused`` with several running, or with none (or no ``proc`` to look
    in: not Linux) and no ``--config``; a bot whose command line the bot's
    parser does not read is the bot, but its ``--config`` cannot be told:
    refused unless ``--config`` is given."""
    ask = (f"Pass the bot's --config (and its --env, if it was given one). The presets here: {presets(root)}. "
           f"{UNCHANGED}")
    if not proc.is_dir():
        seen = f"The running bot is not looked for: there is no {proc} here (not Linux)."
        if not config_given:
            raise Refused(f"{seen} {ask}")
        return Found(None, seen, looked=False)
    bots, unread = find_bots(proc, root)
    seen = _bots_seen(bots, unread, root)
    if len(bots) > 1:
        raise Refused(f"{seen} This renews one bot's login: stop the others first. {UNCHANGED}")
    if not bots and not config_given:
        raise Refused(f"{seen} {ask}")
    if bots and bots[0].unparsed is not None and not config_given:
        raise Refused(f"{seen} Its --config and --env cannot be told from that command line. Pass the bot's "
                      f"--config (and its --env, if it was given one): it is used only if it names the token store "
                      f"the bot holds open. The presets here: {presets(root)}. {UNCHANGED}")
    return Found(bots[0] if bots else None, seen, looked=True)


def resolve_settings(config_arg: Path | None, env_arg: Path | None, bot: BotProcess | None) -> Settings:
    """The config and .env to read, in the working directory (the bot's,
    when ``bot``): the running bot's, or this script's ``--config`` and
    ``--env`` (``--config`` alone: the .env is found as the bot finds it;
    ``--env`` alone: with the bot's config), which ``load_settings`` checks
    against the bot's: for a bot whose command line its parser does not
    read, its ``--config`` and ``--env`` as ``_given`` reads them."""
    if bot is None:
        env_found = "--env" if env_arg is not None else "found as the bot finds it: no --env given"
        return Settings(config_arg, env_arg, "--config", env_found, bot)
    if bot.config is None:
        bot_config = _in_folder(bot.cwd, default_config_path())
        bot_config_found = f"the default of the running bot, pid {bot.pid}, started with no --config"
    else:
        bot_config = _in_folder(bot.cwd, bot.config)
        bot_config_found = f"the --config of the running bot, pid {bot.pid}"
    bot_env = _in_folder(bot.cwd, bot.env) if bot.env is not None else None
    bot_env_found = (f"the --env of the running bot, pid {bot.pid}" if bot.env is not None
                     else "found as the bot finds it: the running bot has no --env")
    if config_arg is not None:
        if env_arg is not None:
            return Settings(config_arg, env_arg, "--config", "--env", bot, bot_config, bot_env, "--config and --env")
        return Settings(config_arg, None, "--config", "found as the bot finds it: no --env given", bot, bot_config,
                        bot_env, "--config")
    if env_arg is not None:
        return Settings(bot_config, env_arg, bot_config_found, "--env", bot, bot_config, bot_env, "--env")
    return Settings(bot_config, bot_env, bot_config_found, bot_env_found, bot, bot_config, bot_env)


def bot_files(settings: Settings) -> list[Path]:
    """The files the running bot read its settings from, as this run finds
    them: its config and the .env ``load_config`` reads with it
    (``dotenv_path``, in the bot's working directory); for a bot whose own
    config cannot be told, the ``--config`` and ``--env`` given, which
    stand for them."""
    if settings.bot_config is not None:
        config, env = settings.bot_config, settings.bot_env
    else:
        config, env = settings.config, settings.env
    dotenv = dotenv_path(config, env)
    return [config] if dotenv is None else [config, dotenv]


def unchanged_since_start(files: list[Path], bot: BotProcess) -> None:
    """``Refused`` when one of ``files`` changed after the running bot
    started: its modification or status-change time, or a symbolic link's
    own (re-pointed), is later. The bot runs on what it read then, and
    /proc keeps only its environment as it was, so this run would read
    another config or .env than the bot's (another token store, another
    app). A file put back with its old modification time (mv, cp -p,
    rsync -a) still has a new status-change time, which nothing sets back;
    so does one whose mode or owner changed. A file that does not exist is
    left to the load, which names it."""
    for path in files:
        try:
            link, target = path.lstat(), path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Refused(f"{path} could not be looked at ({type(exc).__name__}: {exc}): whether it changed since "
                          f"the running bot, pid {bot.pid}, started cannot be told. {UNCHANGED}") from exc
        changed = max(link.st_mtime_ns, link.st_ctime_ns, target.st_mtime_ns, target.st_ctime_ns)
        if changed > bot.started:
            raise Refused(f"{path} changed at {_et_ns(changed)}, after the running bot, pid {bot.pid}, started "
                          f"({_et_ns(bot.started)}): the bot runs on the file as it read it then, and this run would "
                          f"read it as it is now. Restart the bot, which then reads the file as it is (past the "
                          f"store's login time, which a bot starting cannot answer: stop it, run this with its "
                          f"--config, then start it). {UNCHANGED}")


def _bot_environment(proc: Path, pid: int) -> dict[str, str]:
    """The ``CREDENTIALS`` variables of the environment the bot ``pid`` was
    started with (``proc``/<pid>/environ, which this user can read for a
    process of its own), else ``Refused``: its credentials cannot be
    told."""
    try:
        block = (proc / str(pid) / "environ").read_bytes()
    except OSError as exc:
        raise Refused(f"the environment of the running bot, pid {pid}, could not be read ({type(exc).__name__}: "
                      f"{exc}): which app key, app secret and key it runs with cannot be told. {UNCHANGED}") from exc
    names = {name for _field, _label, name in CREDENTIALS}
    found: dict[str, str] = {}
    for entry in block.split(b"\0"):
        name, sign, value = os.fsdecode(entry).partition("=")
        if sign and name in names:
            found[name] = value
    return found


@contextmanager
def _credentials_in_environ(environment: dict[str, str]) -> Iterator[None]:
    """The ``CREDENTIALS`` variables of this process set to
    ``environment``'s (one it lacks unset), then put back."""
    names = [name for _field, _label, name in CREDENTIALS]
    before = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            if name in environment:
                os.environ[name] = environment[name]
            else:
                os.environ.pop(name, None)
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _load(config: Path, env: Path | None, strategy: str | None = None) -> BotConfig:
    return load_config(str(config), strategy_override=strategy, env_path=str(env) if env is not None else None)


def _sources(config: BotConfig, config_path: Path, env: Path | None, environment: dict[str, str],
             where: str) -> dict[str, str]:
    """Where the loader took the app key, the app secret and the key from
    (``config.load_config``: the config's schwab section, else the process
    environment, ``environment`` here, said as ``where``, else the .env it
    read, which put its values in this process's); read while that
    environment is set."""
    dotenv = dotenv_path(config_path, env)
    sources: dict[str, str] = {}
    for field, _label, name in CREDENTIALS:
        value = getattr(config.schwab, field)
        if value is None:
            sources[field] = "none"
        elif environment.get(name, "").strip() == value:
            sources[field] = f"{name} in {where}"
        elif dotenv is not None and os.environ.get(name, "").strip() == value:
            sources[field] = f"{name} in {dotenv}"
        else:
            sources[field] = f"schwab.{field} in {config_path}"
    return sources


def _same_as_the_bots(given: BotConfig, own: BotConfig, settings: Settings) -> None:
    """``Refused`` when the ``--config`` or ``--env`` given names another
    token store than the running bot's, or gives other credentials than
    the bot runs with: the renewal would not reach it."""
    bot = settings.bot
    given_store = token_store_path(given.schwab.tokens_db).resolve()
    own_store = token_store_path(own.schwab.tokens_db).resolve()
    if given_store != own_store:
        raise Refused(f"the running bot, pid {bot.pid}, uses the token store {own_store}, and the {settings.given} "
                      f"given names {given_store}: this renewal would not reach the bot. Run it with no --config or "
                      f"--env, or stop the bot first. {UNCHANGED}")
    other = [label for field, label, _name in CREDENTIALS
             if getattr(given.schwab, field) != getattr(own.schwab, field)]
    if other:
        raise Refused(f"the {settings.given} given gives another {' and '.join(other)} than the running bot, pid "
                      f"{bot.pid}, runs with: the bot could not use the tokens. Run it with no --config or --env. "
                      f"{UNCHANGED}")


def _same_file(one: Path | None, other: Path | None) -> bool:
    return one is not None and other is not None and one.resolve() == other.resolve()


def load_settings(settings: Settings, proc: Path) -> Loaded:
    """The config, loaded as the bot loads it (``load_config``), with the
    app key, the app secret and the key from the environment the running
    bot was started with (``_bot_environment``) over its .env, or, with no
    bot, from this shell's environment over the .env; and where each came
    from. With a bot and a ``--config`` or ``--env`` given, the bot's own
    config is loaded first, then the given one as the bot would load it
    (the bot's environment over the given .env), which must name its token
    store and give its credentials (``_same_as_the_bots``). The loader
    reads a .env once per process: a given .env that is the bot's gives the
    bot's values again. ``Refused`` when a config does not load."""
    bot = settings.bot
    if bot is None:
        environment = {name: os.environ[name] for _field, _label, name in CREDENTIALS if name in os.environ}
        where = THIS_SHELL
    else:
        environment = _bot_environment(proc, bot.pid)
        where = f"the running bot's environment (pid {bot.pid})"
    own = None
    before_given = environment
    if settings.bot_config is not None:
        with _credentials_in_environ(environment):
            try:
                own = _load(settings.bot_config, settings.bot_env, bot.strategy)
            except Exception as exc:
                raise Refused(f"the running bot's config {settings.bot_config} (pid {bot.pid}) could not be loaded "
                              f"({type(exc).__name__}: {exc}): its token store and credentials cannot be told. "
                              f"{UNCHANGED}") from exc
            sources = _sources(own, settings.bot_config, settings.bot_env, environment, where)
            read = {name: os.environ[name] for _field, _label, name in CREDENTIALS if name in os.environ}
        if (settings.config, settings.env) == (settings.bot_config, settings.bot_env):
            return Loaded(own, sources)
        if _same_file(dotenv_path(settings.config, settings.env), dotenv_path(settings.bot_config, settings.bot_env)):
            before_given = read
    with _credentials_in_environ(before_given):
        try:
            config = _load(settings.config, settings.env)
        except Exception as exc:
            raise Refused(f"the config {settings.config} could not be loaded ({type(exc).__name__}: {exc}). "
                          f"{UNCHANGED}") from exc
        sources = _sources(config, settings.config, settings.env, environment, where)
    if own is not None:
        _same_as_the_bots(config, own, settings)
    return Loaded(config, sources)


# ---- the token store ------------------------------------------------------

def _is_sqlite(entry: Path) -> bool:
    """Whether the open file ``entry`` (a /proc/<pid>/fd link) starts as an
    sqlite database does. Read in this process before it opens the store
    through sqlite: it holds no lock on it to drop."""
    try:
        with open(entry, "rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False    # closed meanwhile, or not readable: not named


def check_held(proc: Path, bot: BotProcess, path: Path) -> None:
    """``Refused`` unless the running bot holds the token store ``path``
    open: one of its descriptors (``proc``/<pid>/fd, which this user can
    read for a process of its own) is the same file, by device and inode
    (schwabdev keeps its connection to the store open while its client
    lives). Otherwise the renewal would not reach the bot: a store replaced
    by a copy or a rename since the bot opened it (the bot holds the old
    file, "(deleted)" when it was unlinked), or a store this run finds
    elsewhere (a config changed with its time kept, another ``--config``,
    another home folder for a ``~`` path). The refusal names both: the
    path's file, and the sqlite files the bot holds open."""
    try:
        wanted = os.stat(path)
    except FileNotFoundError:
        wanted = None
    except OSError as exc:
        raise Refused(f"the token store {path} could not be looked at ({type(exc).__name__}: {exc}). "
                      f"{UNCHANGED}") from exc
    folder = proc / str(bot.pid) / "fd"
    try:
        entries = sorted(folder.iterdir(), key=lambda entry: int(entry.name))
    except (OSError, ValueError) as exc:
        raise Refused(f"the files the running bot, pid {bot.pid}, holds open could not be read "
                      f"({type(exc).__name__}: {exc}): whether it holds the token store {path} cannot be told. "
                      f"{UNCHANGED}") from exc
    held: dict[tuple[int, int], str] = {}
    for entry in entries:
        try:
            info = os.stat(entry)
        except OSError:
            continue    # closed meanwhile
        if wanted is not None and (info.st_dev, info.st_ino) == (wanted.st_dev, wanted.st_ino):
            return
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in held and _is_sqlite(entry):
            try:
                target = os.readlink(entry)
            except OSError:
                continue    # closed meanwhile
            held[(info.st_dev, info.st_ino)] = f"{target} (device {info.st_dev}, inode {info.st_ino})"
    this = f"device {wanted.st_dev}, inode {wanted.st_ino}" if wanted is not None else "no such file"
    holds = "; ".join(held.values()) if held else "none"
    raise Refused(f"the running bot, pid {bot.pid}, does not hold the token store {path} ({this}) open; the sqlite "
                  f"files it holds open: {holds}. This renewal would not reach the bot: the store was replaced (by a "
                  f"copy or a rename) since the bot opened it, or this run's settings name another one. Restart the "
                  f"bot (it then opens the store its settings name), or run this with the bot's own settings. "
                  f"{UNCHANGED}")


def permissions_warning(path: Path) -> str | None:
    """A warning when the store's group or others may read or write it."""
    if os.name != "posix":
        return None
    mode = path.stat().st_mode
    if not mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        return None
    return (f"WARNING: {path} can be read or written by its group or others ({stat.filemode(mode)}), and it holds "
            f"the Schwab tokens. Fix: chmod 600 {path}")


def _copied_row(path: Path) -> tuple | None:
    """The store's row (the refresh token's issue time, then the access,
    refresh and id tokens as stored), read from a copy of the store in
    memory: sqlite's backup, from a read-only connection holding the read
    lock it waited 1 s for (a backup left to take it retries a lock held
    by another process for as long as that holds it)."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as source, \
            closing(sqlite3.connect(":memory:")) as copy:
        source.execute("BEGIN")
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()
        source.backup(copy)
        source.rollback()
        return copy.execute("SELECT refresh_token_issued, access_token, refresh_token, id_token FROM schwabdev "
                            "LIMIT 1").fetchone()


def tokens_kept(path: Path, stored: list[str], encryption: str | None, key_source: str) -> str:
    """How the stored tokens are kept, when the configured key reads them as
    schwabdev reads them (``Tokens._dec``, with ``Tokens.__init__``'s key),
    else ``Refused``: tokens in clear with a key configured (schwabdev would
    store the new ones encrypted, which the bot that wrote them, keyless,
    cannot read), encrypted tokens with no key (schwabdev would read the
    store as empty and start a new login, stored in clear), and another
    key. ``key_source``: where the key came from (``_sources``), said in a
    refusal; one from this shell's environment is to be unset."""
    unset = ", and unset SCHWAB_ENCRYPTION_KEY in this shell" if key_source.endswith(THIS_SHELL) else ""
    cipher = None
    if encryption and len(encryption) >= SHORTEST_KEY:
        try:
            cipher = Fernet(encryption)
        except ValueError as exc:
            raise Refused(f"this run's key ({key_source}) is not a Fernet key ({type(exc).__name__}: {exc}). "
                          f"{UNCHANGED}") from exc
    no_key = (f"this run's key ({key_source}) has 16 characters or fewer, which schwabdev takes for none"
              if encryption and not cipher else "this run has no key")
    values = [value for value in stored if value]
    encrypted = [value for value in values if value.startswith(ENCRYPTED_PREFIX)]
    if encrypted and len(encrypted) < len(values):
        raise Refused(f"the token store {path} holds some tokens encrypted and some in clear, which schwabdev never "
                      f"writes. {UNCHANGED}")
    if not encrypted:
        if cipher is not None:
            raise Refused(f"the token store {path} holds its tokens in clear, so the bot that wrote them runs with no "
                          f"key, and this run has one ({key_source}): schwabdev would store the new tokens "
                          f"encrypted, which that bot cannot read. Run with the bot's --config and --env{unset}. "
                          f"{UNCHANGED}")
        return f"in clear ({no_key})"
    if cipher is None:
        raise Refused(f"the token store {path} holds its tokens encrypted, and {no_key} (schwab.encryption, or "
                      f"SCHWAB_ENCRYPTION_KEY in the bot's environment or .env): schwabdev would read the store as "
                      f"empty and start a new login, stored in clear. Run with the bot's --config and --env. "
                      f"{UNCHANGED}")
    try:
        for value in encrypted:
            cipher.decrypt(value[len(ENCRYPTED_PREFIX):].encode())
    except InvalidToken as exc:
        raise Refused(f"the tokens in {path} do not decrypt with this run's key ({key_source}; "
                      f"{type(exc).__name__}): the bot's key is another. Run with the bot's --config and "
                      f"--env{unset}. {UNCHANGED}") from exc
    return "encrypted (this run's key reads them)"


def check_store(loaded: Loaded, out: TextIO) -> Store:
    """The token store, when the settings fit it (``tokens_kept``) and this
    user can write it and its folder (sqlite writes its journal there);
    else ``Refused``. Read on a copy (``_copied_row``)."""
    config = loaded.config
    tokens_db = config.schwab.tokens_db
    path = token_store_path(tokens_db)
    if not path.is_file():
        raise Refused(f"there is no token store at {path} (schwab.tokens_db {tokens_db!r}, read in {Path.cwd()}): "
                      f"a first login is the bot's own first start. {UNCHANGED}")
    warning = permissions_warning(path)
    if warning:
        print(warning, file=out)
    if not (os.access(path, os.R_OK | os.W_OK) and os.access(path.parent, os.W_OK | os.X_OK)):
        raise Refused(f"this user cannot write the token store {path} (owner {_owner(path)}) or its folder, where "
                      f"sqlite writes its journal: Schwab would take the code and issue tokens that could not be "
                      f"stored. Run this as the user the bot runs as. {UNCHANGED}")
    try:
        row = _copied_row(path)
    except sqlite3.Error as exc:
        locked = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
        hint = (" Another process holds it: the bot writing its tokens (try again), or a bot waiting at "
                "schwabdev's own login prompt (paste the URL there instead)." if locked else "")
        raise Refused(f"could not read the token store {path} ({type(exc).__name__}: {exc}).{hint} "
                      f"{UNCHANGED}") from exc
    if row is None:
        raise Refused(f"the token store {path} holds no token: a first login is the bot's own first start. "
                      f"{UNCHANGED}")
    try:
        window = RefreshTokenWindow(stored_time(row[0]))
    except ValueError as exc:
        raise Refused(f"the token store {path} holds a refresh token issue time that does not read "
                      f"({type(exc).__name__}: {exc}). {UNCHANGED}") from exc
    kept = tokens_kept(path, [str(value or "") for value in row[1:]], config.schwab.encryption,
                       loaded.sources["encryption"])
    return Store(path, window, kept)


def window_lines(path: Path, window: RefreshTokenWindow) -> list[str]:
    return [f"The Schwab refresh token in {path}:",
            f"  issued   {_et(window.issued_at)}",
            f"  expires  {_et(window.expires_at)}",
            f"  schwabdev asks for a new login from {_et(window.login_at)}"]


# When a bot that started before ``TAKER`` last changed takes a renewal:
# schwabdev's own reload of the store, before it refreshes a token.
AT_ITS_REFRESH = ("at its next access-token refresh (within 30 minutes during a session; after an idle stretch, at "
                  "its first call)")


def bot_line(found: Found) -> str:
    """Which bot takes the renewal, and when."""
    bot = found.bot
    if bot is not None:
        if bot.before_taker:
            return (f"pid {bot.pid}, working in {bot.cwd}, started before {TAKER} changed: it takes the new tokens "
                    f"from the store {AT_ITS_REFRESH}")
        return f"pid {bot.pid}, working in {bot.cwd}: it takes the new tokens from the store at its next Schwab call"
    if found.looked:
        return "none running: the bot reads the store at its next start"
    return "not looked for: a running bot takes the new tokens at its next Schwab call, a stopped one at its start"


def summary_lines(settings: Settings, loaded: Loaded, found: Found, store: Store) -> list[str]:
    dotenv = dotenv_path(settings.config, settings.env)
    return [f"  config      {settings.config} ({settings.config_found})",
            f"  .env        {dotenv if dotenv is not None else 'none'} ({settings.env_found})",
            *(f"  {label:<10}  {loaded.sources[field]}" for field, label, _name in CREDENTIALS),
            f"  tokens      {store.kept}",
            f"  bot         {bot_line(found)}",
            *window_lines(store.path, store.window)]


def renew_by_line(config: BotConfig, window: RefreshTokenWindow, now: datetime) -> str:
    """When to renew next: before schwabdev's login time, and since the bot
    makes no Schwab call between sessions, by the close of the last session
    ahead of it (``engine.session_ends_after``) or in the idle hours after
    that close."""
    if now >= window.login_at:
        return f"schwabdev has asked for a new login since {_et(window.login_at)}: renew now."
    last_end = None
    for end in session_ends_after(config, now):
        if end >= window.login_at:
            break
        last_end = end
    if last_end is None:
        return "Renew before then: no session ends before then, so renew now."
    return (f"Renew before then: by the close of the last session ahead of it ({_et(last_end)}), or after that "
            f"close while the bot idles.")


# ---- the login ------------------------------------------------------------

def authorize_url(app_key: str, callback_url: str) -> str:
    """The login URL schwabdev's own login prompt prints for this app."""
    return f"{AUTHORIZE_URL}?client_id={app_key}&redirect_uri={callback_url}"


def pasted_url(pasted: str, callback_url: str) -> str:
    """The pasted address-bar URL, stripped, when it is on the callback URL
    and holds a code (as schwabdev reads it, ``Tokens._exchange_auth_code``);
    else ``Refused``. Neither the URL nor its code is printed."""
    url = pasted.strip()
    parts, callback = urlsplit(url), urlsplit(callback_url)
    if (parts.scheme.lower(), parts.netloc.lower()) != (callback.scheme.lower(), callback.netloc.lower()):
        raise Refused(f"the pasted text is not a URL on the callback URL {callback_url}: paste the whole "
                      f"address-bar URL the login lands on ({callback_url}/?code=...). {UNCHANGED}")
    if not parse_qs(parts.query).get("code", [""])[0]:
        raise Refused(f"the pasted URL holds no code: paste the address-bar URL the login lands on, after you log "
                      f"in, not the login URL. {UNCHANGED}")
    return url


def confirm() -> None:
    try:
        answer = input(CONFIRM_PROMPT)
    except (EOFError, KeyboardInterrupt) as exc:
        raise Refused(f"no answer ({type(exc).__name__}). {UNCHANGED}") from exc
    if answer.strip().lower() not in ("y", "yes"):
        raise Refused(f"not confirmed. {UNCHANGED}")


class _Collected(logging.Handler):
    """schwabdev's ERROR and CRITICAL lines during the renewal, printed only
    when it fails (its WARNING on a forced renewal reads "The refresh token
    is expiring soon", whatever is left of it)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


def renew(config: BotConfig, store: Store, found: Found, out: TextIO) -> int:
    """schwabdev's client on the store, then the login URL, the paste and
    the forced renewal, which asks for the login once. The client's
    ``call_on_auth`` hands schwabdev nothing before the URL is pasted: its
    start, when the store's refresh token is past its login time, asks for
    a login, gets none and goes on."""
    schwab = config.schwab
    pasted: list[str] = []
    handed: list[str] = []

    def call_on_auth(auth_url: str) -> str | None:
        if not pasted:
            return None
        handed.append(auth_url)
        return pasted[0]

    collected = _Collected()
    logger = logging.getLogger("Schwabdev")
    propagate = logger.propagate
    logger.addHandler(collected)
    logger.propagate = False
    error = None
    try:
        try:
            client = schwabdev.Client(app_key=schwab.app_key, app_secret=schwab.app_secret,
                                      callback_url=schwab.callback_url, tokens_db=str(store.path),
                                      encryption=schwab.encryption, timeout=schwab.timeout,
                                      call_on_auth=call_on_auth, open_browser_for_auth=False)
        except (Exception, KeyboardInterrupt) as exc:
            print(f"\nNot renewed: schwabdev's client could not start on the token store ({type(exc).__name__}: "
                  f"{exc}).", file=out)
            for line in collected.lines:
                print(f"  schwabdev: {line}", file=out)
            held = "locked" in str(exc)
            print(("Another process held the store's lock (the bot writing its tokens). " if held else "")
                  + "No code was sent: run this script again.", file=out)
            return 1
        try:
            login_url = authorize_url(schwab.app_key, schwab.callback_url)
            print(f"\n1. Open this URL in a browser and log in to Schwab:\n\n   {login_url}\n", file=out)
            print(f"2. The login lands on {schwab.callback_url}/?code=..., a page that does not load. Copy the whole "
                  f"address-bar URL and paste it here within about 30 seconds (the code expires).", file=out)
            try:
                pasted_text = input(PASTE_PROMPT)
            except (EOFError, KeyboardInterrupt) as exc:
                raise Refused(f"no URL was pasted ({type(exc).__name__}). {UNCHANGED}") from exc
            pasted.append(pasted_url(pasted_text, schwab.callback_url))
            try:
                client.update_tokens(force_refresh_token=True)
            except (Exception, KeyboardInterrupt) as exc:
                error = f"{type(exc).__name__}: {exc}"
        finally:
            client.close()
    finally:
        logger.removeHandler(collected)
        logger.propagate = propagate
    return _report(config, store, found, error, collected.lines, bool(handed), out)


def _taken_line(found: Found) -> str:
    """Who takes the new tokens, and when."""
    bot = found.bot
    if bot is not None:
        if bot.before_taker:
            return (f"The running bot, pid {bot.pid}, started before {TAKER} changed: it takes the new tokens from "
                    f"the store {AT_ITS_REFRESH}, no restart.")
        return (f"The running bot, pid {bot.pid}, takes the new tokens from the store at its next Schwab call, no "
                f"restart (its log: \"Schwab tokens taken from the token store\").")
    if found.looked:
        return "No bot is running: the bot reads the new tokens from the store at its next start."
    return ("A running bot takes the new tokens from the store at its next Schwab call, no restart (its log: \"Schwab "
            "tokens taken from the token store\"); a stopped one reads them at its next start.")


def _report(config: BotConfig, store: Store, found: Found, error: str | None, collected: list[str], handed: bool,
            out: TextIO) -> int:
    """Whether the store's refresh token is newer than before the renewal,
    read back from the store: the new window, or why not."""
    def _why() -> None:
        if error:
            print(f"  {error}", file=out)
        for line in collected:
            print(f"  schwabdev: {line}", file=out)

    try:
        after = read_refresh_token_window(str(store.path))
    except (sqlite3.Error, ValueError) as exc:
        print("\nThe renewal ended:", file=out)
        _why()
        print(f"Could not read the token store after it ({type(exc).__name__}: {exc}): run this script with --status "
              f"to see whether it took.", file=out)
        return 1
    if after.issued_at > store.window.issued_at:
        print("\nRenewed." if handed else "\nRenewed by another process meanwhile: the URL you pasted was not "
              "used.", file=out)
        for line in window_lines(store.path, after):
            print(line, file=out)
        print(_taken_line(found), file=out)
        print(renew_by_line(config, after, sessions.now_et()), file=out)
        return 0
    print(f"\nNot renewed: the token store still holds the refresh token issued {_et(store.window.issued_at)}.",
          file=out)
    _why()
    if handed:
        print("Schwab did not take the code, or the new tokens could not be stored. A code lasts about 30 "
              "seconds: run this script again, log in again and paste the new URL at once.", file=out)
    else:
        print("The code was not exchanged: schwabdev stopped before it (above). Run this script again and log "
              "in again.", file=out)
    return 1


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        description="Renew the Schwab login (schwabdev's 7-day refresh token) from a terminal while the bot runs.")
    parser.add_argument("--config", default=None,
                        help="the bot's YAML config (default: the --config of the bot of this checkout that runs; "
                             "checked against it)")
    parser.add_argument("--env", default=None,
                        help="the bot's .env (default: the running bot's --env, or found as the bot finds it)")
    parser.add_argument("--status", action="store_true",
                        help="the checks and the summary only: read-only, no login")
    args = parser.parse_args(argv)
    config_arg, env_arg = _absolute(args.config), _absolute(args.env)
    try:
        if not args.status and (sys.stdin is None or not sys.stdin.isatty()):
            raise Refused(NOT_A_TERMINAL)
        found = look_for_the_bot(config_arg is not None, REPO_ROOT, PROC)
        print(found.seen, file=out)
        if found.bot is not None:
            try:
                os.chdir(found.bot.cwd)
            except OSError as exc:
                raise Refused(f"the running bot's working directory {found.bot.cwd} cannot be entered "
                              f"({type(exc).__name__}: {exc}). {UNCHANGED}") from exc
        settings = resolve_settings(config_arg, env_arg, found.bot)
        if found.bot is not None:
            unchanged_since_start(bot_files(settings), found.bot)
        loaded = load_settings(settings, PROC)
        if found.bot is not None:
            check_held(PROC, found.bot, token_store_path(loaded.config.schwab.tokens_db))
        store = check_store(loaded, out)
        for line in summary_lines(settings, loaded, found, store):
            print(line, file=out)
        if args.status:
            print(renew_by_line(loaded.config, store.window, sessions.now_et()), file=out)
            return 0
        confirm()
        return renew(loaded.config, store, found, out)
    except Refused as exc:
        print(f"Refused: {exc}", file=out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
