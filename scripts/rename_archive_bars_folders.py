# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""Rename an older session archive's bars folders, and the manifest keys
that name them, to the names the bot writes since 2026-10-11.

    .venv/bin/python scripts/rename_archive_bars_folders.py SESSIONS_DIR [--apply]

with the bot's Python (here README_LINUX_DEPLOY.md's venv; the script needs
only the standard library). SESSIONS_DIR is the archive root,
``{log_dir}/sessions`` (``.logs/sessions`` by default). Without ``--apply``
nothing is changed and each day's plan is printed. Run it with the bot
stopped, as the user that owns the archive.

In each ``SESSIONS_DIR/YYYY-MM-DD/bars/``:

- ``1m`` (the merged 1m frame) stays;
- ``{N}m`` with N above 1 (the merged 1m frame resampled to N minutes)
  becomes ``{N}m_from_1m``;
- ``htf_{N}m`` (the stored HTF frame: Schwab's N-minute price history)
  becomes ``{N}m_schwab_htf``.

A folder is renamed by its name, so one the manifest's keys do not list is
renamed too. In ``manifest.json`` the same names are renamed in the keys of
``bars_written_by_timeframe`` and ``bars_skipped_by_timeframe``, in their
order, and the file is written as the bot writes it
(``json.dumps(manifest, indent=2, default=str)``), so nothing else in it
changes; ``timeframes_exported`` names timeframes, not folders, and stays.

A day is refused, and left as it is, when it holds both the old and the new
name of one series (a folder, or a key of one of the two dicts): the two are
never merged, and the refusal names every such pair. It is refused too when
a folder or file it would change is a symlink (no symlink is followed) or
not what it should be, or when its ``manifest.json`` is not as the bot
writes it; a refused day prints only why. Another name in ``bars/`` or in
those dicts is left alone and reported. A day already in the new layout,
or without ``bars/``, is skipped, so a second run changes nothing.

Each day is changed whole or not at all: the new manifest is written to
``manifest.json.rename_bars.tmp`` beside it first, then the folders are
renamed, then the file replaces ``manifest.json``; an error part-way renames
the folders back and removes the file. A run killed part-way leaves a day
that a re-run finishes (the folders not yet renamed, the manifest, and the
file, which it removes). Nothing outside ``bars/`` and ``manifest.json`` is
touched, but for that file.

Every change is printed, then a summary. The exit status is 1 when a day
was refused or failed, else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OLD_RESAMPLED = re.compile(r"^([1-9][0-9]*)m$")
OLD_SCHWAB_HTF = re.compile(r"^htf_([1-9][0-9]*)m$")
NEW_RESAMPLED = re.compile(r"^([1-9][0-9]*)m_from_1m$")
NEW_SCHWAB_HTF = re.compile(r"^([1-9][0-9]*)m_schwab_htf$")
MERGED_1M = "1m"
MANIFEST = "manifest.json"
MANIFEST_TMP = "manifest.json.rename_bars.tmp"
MANIFEST_FIELDS = ("bars_written_by_timeframe", "bars_skipped_by_timeframe")


def new_name(name: str) -> str | None:
    """The name an old-layout folder (or manifest key) takes, else None."""
    match = OLD_RESAMPLED.match(name)
    if match and int(match.group(1)) > 1:
        return f"{match.group(1)}m_from_1m"
    match = OLD_SCHWAB_HTF.match(name)
    if match:
        return f"{match.group(1)}m_schwab_htf"
    return None


def is_new_name(name: str) -> bool:
    match = NEW_RESAMPLED.match(name)
    return bool(match and int(match.group(1)) > 1) or bool(NEW_SCHWAB_HTF.match(name))


class Refused(Exception):
    """The day is left as it is."""


def _both(pairs: list[tuple[str, str]]) -> str:
    """``both A and B, and both C and D``: every pair of a series' two names."""
    return ", and ".join(f"both {old} and {new}" for old, new in pairs)


@dataclass
class DayPlan:
    day: str
    status: str = "nothing"            # rename | already_new | nothing | no_bars | refused
    folders: list[tuple[str, str]] = field(default_factory=list)
    keys: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    unlisted: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    stale_tmp: bool = False
    manifest_bytes: bytes | None = None
    manifest_mode: int | None = None
    manifest_owner: tuple[int, int] | None = None
    why: str = ""

    def n_keys(self) -> int:
        return sum(len(pairs) for pairs in self.keys.values())


def _is_plain_dir(path: Path) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def _plan_manifest(day_dir: Path, plan: DayPlan) -> tuple[list[str] | None, bool]:
    """The manifest's renames into ``plan``; the old names its dicts list
    (None without a manifest), and whether they list a new one. Raises
    Refused."""
    path = day_dir / MANIFEST
    if not os.path.lexists(path):
        return None, False
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{MANIFEST} is not a regular file (a symlink is not followed)")
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"{MANIFEST} does not read as JSON ({type(exc).__name__}: {exc})") from exc
    if not isinstance(manifest, dict):
        raise Refused(f"{MANIFEST} is not a JSON object")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    if _manifest_bytes(manifest, newline) != raw:
        raise Refused(f"{MANIFEST} is not as the bot writes it (json.dumps(manifest, indent=2, default=str)), "
                      "so it is not rewritten")
    listed: list[str] = []
    new_listed = False
    changed = False
    both: list[str] = []
    for name in MANIFEST_FIELDS:
        if name not in manifest:
            continue
        counts = manifest[name]
        if not isinstance(counts, dict):
            raise Refused(f"{MANIFEST}'s {name} is not an object")
        renamed: dict = {}
        pairs: list[tuple[str, str]] = []
        twins: list[tuple[str, str]] = []
        for key, value in counts.items():
            target = new_name(key)
            if target is not None and target in counts:
                twins.append((key, target))
            elif target is not None:
                pairs.append((key, target))
                listed.append(key)
            elif is_new_name(key):
                new_listed = True
            elif key != MERGED_1M:
                plan.unknown.append(f"{MANIFEST} {name}: {key}")
            renamed[target or key] = value
        if twins:
            both.append(f"{name} holds {_both(twins)}")
        if pairs:
            plan.keys[name] = pairs
            manifest[name] = renamed
            changed = True
    if both:
        raise Refused(f"{MANIFEST}'s {'; '.join(both)}: they are not merged")
    if changed:
        plan.manifest_bytes = _manifest_bytes(manifest, newline)
        plan.manifest_mode = stat.S_IMODE(st.st_mode)
        plan.manifest_owner = (st.st_uid, st.st_gid)
    return listed, new_listed


def _manifest_bytes(manifest: dict, newline: str) -> bytes:
    """``session_archive._write_manifest``'s text, as ``write_text`` writes
    it on the platform whose newline the file has."""
    return json.dumps(manifest, indent=2, default=str).replace("\n", newline).encode("utf-8")


def plan_day(day_dir: Path) -> DayPlan:
    plan = DayPlan(day_dir.name)
    try:
        if os.path.islink(day_dir):
            raise Refused("the day folder is a symlink (not followed)")
        bars = day_dir / "bars"
        if not os.path.lexists(bars):
            plan.status = "no_bars"
            return plan
        if not _is_plain_dir(bars):
            raise Refused("bars is not a folder (a symlink is not followed)")
        tmp = day_dir / MANIFEST_TMP
        if os.path.lexists(tmp):
            if _is_plain_dir(tmp):
                raise Refused(f"{MANIFEST_TMP} is a folder")
            plan.stale_tmp = True
        names = sorted(os.listdir(bars))
        new_present = False
        twins: list[tuple[str, str]] = []
        for name in names:
            if name == MERGED_1M:
                continue
            target = new_name(name)
            if target is None and not is_new_name(name):
                plan.unknown.append(f"bars/{name}")
                continue
            if not _is_plain_dir(bars / name):
                raise Refused(f"bars/{name} is not a folder (a symlink is not followed)")
            if target is None:
                new_present = True
                continue
            if os.path.lexists(bars / target):
                twins.append((name, target))
                continue
            plan.folders.append((name, target))
        if twins:
            raise Refused(f"bars holds {_both(twins)}: they are not merged")
        listed, new_listed = _plan_manifest(day_dir, plan)
        if listed is not None:
            plan.unlisted = [old for old, _new in plan.folders if old not in listed]
        if plan.folders or plan.keys or plan.stale_tmp:
            plan.status = "rename"
        elif new_present or new_listed:
            plan.status = "already_new"
        else:
            plan.status = "nothing"
    except Refused as exc:
        plan.status = "refused"
        plan.why = str(exc)
    except OSError as exc:
        plan.status = "refused"
        plan.why = f"{type(exc).__name__}: {exc}"
    if plan.status == "refused":
        # Nothing of a refused day is changed, so what was planned before the refusal is dropped, not printed.
        return DayPlan(plan.day, status=plan.status, why=plan.why)
    return plan


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_tmp(day_dir: Path, plan: DayPlan) -> Path:
    tmp = day_dir / MANIFEST_TMP
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(plan.manifest_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        if plan.manifest_owner is not None and hasattr(os, "chown"):
            st = os.lstat(tmp)
            if (st.st_uid, st.st_gid) != plan.manifest_owner:
                os.chown(tmp, *plan.manifest_owner)
        os.chmod(tmp, plan.manifest_mode)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def apply_day(day_dir: Path, plan: DayPlan, out) -> str:
    """Apply ``plan``: ``renamed``, or ``failed`` with the day as it was (or,
    when an undo fails too, part-way, which a re-run finishes)."""
    bars = day_dir / "bars"
    done: list[tuple[str, str]] = []
    tmp: Path | None = None
    try:
        if plan.stale_tmp:
            os.unlink(day_dir / MANIFEST_TMP)
            print(f"  removed {MANIFEST_TMP}, left by an interrupted run", file=out)
        if plan.manifest_bytes is not None:
            tmp = _write_tmp(day_dir, plan)
        for old, new in plan.folders:
            if os.path.lexists(bars / new):
                raise Refused(f"bars/{new} appeared during the run")
            os.rename(bars / old, bars / new)
            done.append((old, new))
        if tmp is not None:
            os.replace(tmp, day_dir / MANIFEST)
            tmp = None
    except BaseException as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}", file=out)
        undo_failed = []
        for old, new in reversed(done):
            try:
                os.rename(bars / new, bars / old)
            except OSError as undo_exc:
                undo_failed.append(f"bars/{new} -> bars/{old} ({type(undo_exc).__name__}: {undo_exc})")
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError as undo_exc:
                undo_failed.append(f"remove {MANIFEST_TMP} ({type(undo_exc).__name__}: {undo_exc})")
        if undo_failed:
            print(f"  PART-WAY: could not undo {'; '.join(undo_failed)}; a re-run finishes the day", file=out)
        else:
            print("  undone: the day is as it was", file=out)
        if not isinstance(exc, Exception):
            raise
        return "failed"
    try:
        _fsync_dir(bars)
        _fsync_dir(day_dir)
    except OSError as exc:
        print(f"  WARNING: renamed, but the folders could not be synced to disk ({type(exc).__name__}: {exc})",
              file=out)
    return "renamed"


def _print_plan(plan: DayPlan, apply: bool, out) -> None:
    head = {"rename": "renaming" if apply else "would rename", "already_new": "already in the new layout, skipped",
            "nothing": "nothing to rename", "no_bars": "no bars folder, skipped",
            "refused": f"REFUSED, left as it is: {plan.why}"}[plan.status]
    print(f"{plan.day}: {head}", file=out)
    for old, new in plan.folders:
        note = f" (not listed in {MANIFEST})" if old in plan.unlisted else ""
        print(f"  bars/{old} -> bars/{new}{note}", file=out)
    for name, pairs in plan.keys.items():
        print(f"  {MANIFEST} {name}: " + ", ".join(f"{old} -> {new}" for old, new in pairs), file=out)
    if plan.stale_tmp and plan.status == "rename" and not apply:
        print(f"  remove {MANIFEST_TMP} (left by an interrupted run)", file=out)
    for name in plan.unknown:
        print(f"  {name}: not a name this script renames, left alone", file=out)


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        description="Rename an older session archive's bars folders ({N}m -> {N}m_from_1m, "
                    "htf_{N}m -> {N}m_schwab_htf) and the manifest keys that name them.")
    parser.add_argument("sessions_dir", help="the archive root, {log_dir}/sessions (e.g. .logs/sessions)")
    parser.add_argument("--apply", action="store_true", help="make the changes (without it, only print the plan)")
    args = parser.parse_args(argv)
    root = Path(args.sessions_dir)
    if not root.is_dir():
        parser.error(f"{root} is not a folder")
    print(f"{'Renaming' if args.apply else 'Dry run (nothing is changed; --apply renames)'}: {root}", file=out)
    counts = {"rename": 0, "renamed": 0, "failed": 0, "already_new": 0, "nothing": 0, "no_bars": 0, "refused": 0}
    folders = keys = unknown = 0
    for entry in sorted(os.listdir(root)):
        day_dir = root / entry
        if not DAY_RE.match(entry):
            print(f"{entry}: not a day folder, left alone", file=out)
            continue
        plan = plan_day(day_dir)
        _print_plan(plan, args.apply, out)
        unknown += len(plan.unknown)
        if plan.status != "rename":
            counts[plan.status] += 1
            continue
        if not args.apply:
            counts["rename"] += 1
            folders += len(plan.folders)
            keys += plan.n_keys()
            continue
        result = apply_day(day_dir, plan, out)
        counts[result] += 1
        if result == "renamed":
            folders += len(plan.folders)
            keys += plan.n_keys()
    days = sum(counts.values())
    if args.apply:
        done = f"{counts['renamed']} renamed, {counts['failed']} failed"
        verb = "renamed"
    else:
        done = f"{counts['rename']} to rename"
        verb = "to rename"
    print(f"{days} days: {done}, {counts['already_new']} already in the new layout, {counts['nothing']} with "
          f"nothing to rename, {counts['no_bars']} without bars, {counts['refused']} refused; {folders} folders "
          f"and {keys} manifest keys {verb}; {unknown} other names left alone.", file=out)
    return 1 if counts["refused"] or counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
