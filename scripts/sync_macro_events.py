# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

import yaml

USER_AGENT = "intraday-tv-schwab-bot-macro-sync/1.0"

# Repo root = parent of scripts/. Default output path is anchored here so the
# generated blackout file lands next to config.yaml regardless of the caller's
# working directory. Configs reference it as `./macro_events.auto.yaml` which
# resolves relative to the bot's cwd at runtime (the deploy root).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "macro_events.auto.yaml"
# Release times below are rendered as ET strings into the blackout YAML.

FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/current_year.asp"
# The year after this one, when the horizon reaches into it: the current
# year's page lists this year's releases only (2026-11-08: a sync run in
# November missed January's CPI / NFP / PPI until a run after New Year).
BLS_YEAR_URL = "https://www.bls.gov/schedule/{year}/home.htm"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
CENSUS_ADVANCE_URL = "https://www.census.gov/econ/indicators/release_schedule.html"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class EventRule:
    slug: str
    label: str
    pre_minutes: int
    post_minutes: int
    force_flatten: bool
    default_time: str | None = None


@dataclass(frozen=True)
class RawCalendarEvent:
    source: str
    title: str
    date: dt.date
    release_time: str | None


RULES: dict[str, EventRule] = {
    "FOMC": EventRule("fomc", "FOMC Rate Decision", pre_minutes=15, post_minutes=45, force_flatten=True, default_time="14:00"),
    "CPI": EventRule("cpi", "BLS CPI", pre_minutes=10, post_minutes=75, force_flatten=False),
    "PPI": EventRule("ppi", "BLS PPI", pre_minutes=10, post_minutes=60, force_flatten=False),
    "NFP": EventRule("nfp", "Employment Situation / NFP", pre_minutes=10, post_minutes=75, force_flatten=False),
    "GDP": EventRule("gdp", "BEA GDP", pre_minutes=10, post_minutes=45, force_flatten=False),
    "PCE": EventRule("pce", "Personal Income and Outlays / PCE", pre_minutes=10, post_minutes=45, force_flatten=False),
    "RETAIL": EventRule("retail_sales", "Retail Sales", pre_minutes=10, post_minutes=45, force_flatten=False),
}


def fetch_url(url: str, timeout: int = 20) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        encoding = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(encoding, errors="replace")


TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WHITESPACE_RE = re.compile(r"[ \t\xa0]+")


def html_to_text(raw_html: str) -> str:
    text = COMMENT_RE.sub(" ", raw_html)
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = TAG_RE.sub("\n", text)
    text = html.unescape(text)
    lines: list[str] = []
    for line in text.splitlines():
        line = WHITESPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


DATE_FULL_RE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2})(?:-(?P<endday>\d{1,2}))?,\s+(?P<year>\d{4})",
    re.I,
)
TIME_RE = re.compile(r"(0?\d|1[0-2]):[0-5]\d\s?(?:AM|PM)", re.I)
WEEKDAY_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>\d{4})",
    re.I,
)


def parse_month_date(month: str, day: str, year: str) -> dt.date:
    return dt.date(int(year), MONTHS[month.lower()], int(day))


def normalize_time(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.strftime("%H:%M")
        except ValueError:
            continue
    return None


MONTH_NAMES = "January|February|March|April|May|June|July|August|September|October|November|December"
FOMC_YEAR_RE = re.compile(r"(?P<year>\d{4}) FOMC Meetings", re.I)
# A meeting's month cell: "January", or "Apr/May" for a meeting across two
# months (the second is the decision day's).
FOMC_MONTH_RE = re.compile(rf"(?:(?:{MONTH_NAMES}|[A-Z][a-z]{{2}})/)?(?P<month>{MONTH_NAMES}|[A-Z][a-z]{{2}})$")
# Its day cell: "27-28", "17-18*" (the asterisk marks a meeting with
# projections), a single "29" for a one-day meeting.
FOMC_DAYS_RE = re.compile(r"(?P<day>\d{1,2})(?:-(?P<endday>\d{1,2}))?\*?$")
MONTH_ABBREVIATIONS = {name[:3].lower(): name for name in MONTHS}


def parse_fomc(html_text: str) -> list[RawCalendarEvent]:
    """The decision days of the Fed's meeting calendar: under each "YYYY
    FOMC Meetings" heading the page lists a month cell then a day cell per
    meeting ("January" / "27-28"), the decision on the last day. Until
    2026-11-08 this read every "Month D, YYYY" on the page, which the
    meeting cells never form: it collected the Minutes release dates
    ("(Released February 18, 2026)") and the footer's date as decisions,
    and no real decision day (R-057)."""
    lines = html_to_text(html_text).splitlines()
    events: list[RawCalendarEvent] = []
    seen: set[dt.date] = set()
    year: int | None = None
    month: str | None = None
    for line in lines:
        heading = FOMC_YEAR_RE.search(line)
        if heading:
            year = int(heading.group("year"))
            month = None
            continue
        if year is None or year < dt.date.today().year - 1:
            continue
        month_cell = FOMC_MONTH_RE.fullmatch(line)
        if month_cell:
            token = month_cell.group("month").lower()
            month = MONTH_ABBREVIATIONS.get(token[:3]) if token[:3] in MONTH_ABBREVIATIONS else None
            if month is None or token not in (month.lower(), month[:3].lower()):
                month = None
            continue
        if month is None:
            continue
        days = FOMC_DAYS_RE.fullmatch(line)
        if not days:
            # Anything else after a month cell (the statement links, the
            # Minutes line) ends that meeting's cells.
            month = None
            continue
        event_date = parse_month_date(month, days.group("endday") or days.group("day"), str(year))
        month = None
        if event_date in seen:
            continue
        seen.add(event_date)
        events.append(RawCalendarEvent("fed", "FOMC", event_date, RULES["FOMC"].default_time))
    return events


BLS_TARGETS = {
    "Consumer Price Index": "CPI",
    "Producer Price Index": "PPI",
    "Employment Situation": "NFP",
}


def parse_bls(html_text: str) -> list[RawCalendarEvent]:
    text = html_to_text(html_text)
    lines = text.splitlines()
    events: list[RawCalendarEvent] = []
    i = 0
    while i < len(lines):
        m = WEEKDAY_DATE_RE.fullmatch(lines[i])
        if not m:
            i += 1
            continue
        event_date = parse_month_date(m.group("month"), m.group("day"), m.group("year"))
        if i + 2 >= len(lines):
            i += 1
            continue
        maybe_time = TIME_RE.fullmatch(lines[i + 1])
        release = lines[i + 2] if maybe_time else None
        if maybe_time and release:
            for title_prefix, key in BLS_TARGETS.items():
                if release.startswith(title_prefix):
                    events.append(RawCalendarEvent("bls", key, event_date, normalize_time(lines[i + 1])))
                    break
            i += 3
            continue
        i += 1
    return events


# The national releases: "GDP (Advance Estimate) ...", "GDP (Second Estimate)
# and Corporate Profits ...", "Personal Income and Outlays, October 2026".
# "GDP by County ..." and the like are not market events.
BEA_TARGETS = {
    "GDP (": "GDP",
    "Personal Income and Outlays": "PCE",
}
BEA_YEAR_RE = re.compile(r"Year (?P<year>\d{4})$")
BEA_DATE_RE = re.compile(rf"(?P<month>{MONTH_NAMES}) (?P<day>\d{{1,2}})$", re.I)
# The release-type cell ("News Release", "Article", "Data Release", "Visual
# Data"), which the page's markup splits into an initial and the rest.
BEA_TYPE_RE = re.compile(r"[A-Z]|ews(?: Release)?|rticle|ata Release|isual Data|News Release|Article|Data Release|Visual Data")


def parse_bea(html_text: str) -> list[RawCalendarEvent]:
    """BEA's release schedule: a "Year YYYY" heading, then per release a
    "Month D" line (no year), its time, its type cell and its title, in
    that order. Until 2026-11-08 this looked for "Month D, YYYY" lines,
    which the page has not carried, and read nothing (R-157); the first
    rewrite paired each date with the line before it, the previous row's
    title (B9's verification)."""
    lines = html_to_text(html_text).splitlines()
    events: list[RawCalendarEvent] = []
    year: int | None = None
    for i, line in enumerate(lines):
        heading = BEA_YEAR_RE.fullmatch(line)
        if heading:
            year = int(heading.group("year"))
            continue
        m = BEA_DATE_RE.fullmatch(line)
        if not m or year is None:
            continue
        j = i + 1
        release_time = None
        if j < len(lines) and TIME_RE.fullmatch(lines[j]):
            release_time = normalize_time(lines[j])
            j += 1
        while j < len(lines) and BEA_TYPE_RE.fullmatch(lines[j]):
            j += 1
        title = lines[j] if j < len(lines) else ""
        for title_prefix, key in BEA_TARGETS.items():
            if title.startswith(title_prefix):
                events.append(RawCalendarEvent("bea", key, parse_month_date(m.group("month"), m.group("day"), str(year)),
                                               release_time))
                break
    return events


def parse_census(html_text: str) -> list[RawCalendarEvent]:
    text = html_to_text(html_text)
    lines = text.splitlines()
    events: list[RawCalendarEvent] = []
    i = 0
    target = "Advance Monthly Sales for Retail and Food Services"
    while i < len(lines):
        if not lines[i].startswith(target):
            i += 1
            continue
        tail = lines[i][len(target):].strip()
        m = DATE_FULL_RE.search(tail)
        date_text = None
        time_text = None
        if m:
            date_text = parse_month_date(m.group("month"), m.group("day"), m.group("year"))
            tm = TIME_RE.search(tail[m.end():])
            if tm:
                time_text = normalize_time(tm.group(0))
        else:
            # Fallback: date on next line, then time on the following line.
            if i + 1 < len(lines):
                m2 = DATE_FULL_RE.search(lines[i + 1]) or WEEKDAY_DATE_RE.search(lines[i + 1])
                if m2:
                    date_text = parse_month_date(m2.group("month"), m2.group("day"), m2.group("year"))
            if i + 2 < len(lines):
                tm = TIME_RE.search(lines[i + 2])
                if tm:
                    time_text = normalize_time(tm.group(0))
        if date_text:
            events.append(RawCalendarEvent("census", "RETAIL", date_text, time_text or "08:30"))
        i += 1
    return events


def classify_event(event: RawCalendarEvent) -> EventRule | None:
    return RULES.get(event.title)


BLACKOUT_HEADER = """# Auto-generated by scripts/sync_macro_events.py\n#\n# This file is safe to overwrite. Put one-off manual overrides in\n# events.blackouts inside your main config instead of editing this file.\n#\n# Sources used by the sync script:\n# - Federal Reserve FOMC calendar\n# - BLS release calendar (this year's, and next year's when the horizon reaches it)\n# - BEA release schedule\n# - Census advance indicators release schedule (only with --census: the page stopped listing releases)\n#\n# Generated at: {generated_at}\n"""


def build_blackout_row(event: RawCalendarEvent, rule: EventRule) -> dict[str, object]:
    release_hhmm = normalize_time(event.release_time or rule.default_time)
    if release_hhmm is None:
        raise ValueError(f"No release time for {event.title} on {event.date.isoformat()}")
    release_dt = dt.datetime.combine(event.date, dt.datetime.strptime(release_hhmm, "%H:%M").time())
    start_dt = release_dt - dt.timedelta(minutes=rule.pre_minutes)
    end_dt = release_dt + dt.timedelta(minutes=rule.post_minutes)
    return {
        "label": rule.label,
        "date": event.date.isoformat(),
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "block_new_entries": True,
        "force_flatten": rule.force_flatten,
    }


def within_horizon(event_date: dt.date, days_ahead: int) -> bool:
    today = dt.date.today()
    return today <= event_date <= today + dt.timedelta(days=days_ahead)


def dedupe_rows(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row.get("label")), str(row.get("date")), str(row.get("start")), str(row.get("end")))
        unique[key] = row
    return sorted(unique.values(), key=lambda x: (str(x.get("date")), str(x.get("start")), str(x.get("label"))))


def sync(days_ahead: int, include_census: bool = False) -> tuple[list[dict[str, object]], list[str]]:
    """The blackout rows of every source inside the horizon, and the
    warnings: a source whose fetch or parse raised, or that PARSED NOTHING
    (2026-11-08: BEA and Census read nothing for months and the file was
    written without them, silently). ``main`` keeps the file as it stands
    when there is a warning."""
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    today = dt.date.today()
    sources = [
        (FED_FOMC_URL, parse_fomc),
        (BLS_CALENDAR_URL, parse_bls),
        (BEA_SCHEDULE_URL, parse_bea),
    ]
    horizon_end = today + dt.timedelta(days=days_ahead)
    if horizon_end.year > today.year:
        sources.append((BLS_YEAR_URL.format(year=today.year + 1), parse_bls))
    if include_census:
        sources.append((CENSUS_ADVANCE_URL, parse_census))
    for url, parser in sources:
        try:
            html_text = fetch_url(url)
            events = parser(html_text)
        except (URLError, HTTPError, TimeoutError, ValueError) as exc:
            warnings.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # pragma: no cover - defensive runtime safety
            warnings.append(f"{url}: unexpected parser error: {type(exc).__name__}: {exc}")
            continue
        if not events:
            warnings.append(f"{url}: parsed no events (the page's layout may have changed)")
            continue
        for event in events:
            if not within_horizon(event.date, days_ahead):
                continue
            rule = classify_event(event)
            if not rule:
                continue
            try:
                rows.append(build_blackout_row(event, rule))
            except Exception as exc:
                warnings.append(f"{event.source}:{event.title}:{event.date.isoformat()}: {exc}")
    return dedupe_rows(rows), warnings


def write_yaml(path: Path, rows: list[dict[str, object]]) -> None:
    header = BLACKOUT_HEADER.format(generated_at=dt.datetime.now().isoformat(timespec="seconds"))
    # The whole payload is dumped: with no rows, cutting "events:\n" off
    # "events: []\n" left "events:\n[]", which is not YAML, and the bot
    # refuses a blackout_file it cannot read. Written beside the file and
    # moved into place (2026-11-08): the running bot re-reads the file on
    # its mtime, and a half-written one is not YAML.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + yaml.safe_dump({"events": rows}, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync official macro calendars into the bot's blackout YAML format.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"Output YAML path. Default: {DEFAULT_OUTPUT} (repo root, next to config.yaml)")
    parser.add_argument("--days-ahead", type=int, default=90, help="Number of days ahead to include. Default: 90")
    parser.add_argument("--census", action="store_true",
                        help="Read the Census retail-sales source too (off by default since 2026-11-08: the page lists "
                             "no releases, so the sync would refuse to write).")
    parser.add_argument("--stdout", action="store_true", help="Print the generated YAML to stdout instead of writing a file.")
    parser.add_argument("--write-partial", action="store_true",
                        help="Write the file although a source failed or parsed nothing (the default keeps the file as "
                             "it stands and exits 1, so a changed page does not silently drop an event family).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, warnings = sync(days_ahead=max(1, args.days_ahead), include_census=args.census)
    payload = {"events": rows}
    if warnings:
        sys.stderr.write("Warnings:\n")
        for warning in warnings:
            sys.stderr.write(f"  - {warning}\n")
    if args.stdout:
        sys.stdout.write(yaml.safe_dump(payload, sort_keys=False))
    elif warnings and not args.write_partial:
        sys.stderr.write(f"Not written: {len(rows)} row(s) from the sources that answered; the file stands as it is "
                         f"(--write-partial writes them anyway).\n")
    else:
        out_path = Path(args.output)
        write_yaml(out_path, rows)
        sys.stderr.write(f"Wrote {len(rows)} blackout event(s) to {out_path}\n")
    return 1 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
