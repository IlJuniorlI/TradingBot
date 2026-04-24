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
EASTERN = dt.timezone(dt.timedelta(hours=-5))

# Repo root = parent of scripts/. Default output path is anchored here so the
# generated blackout file lands next to config.yaml regardless of the caller's
# working directory. Configs reference it as `./macro_events.auto.yaml` which
# resolves relative to the bot's cwd at runtime (the deploy root).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "macro_events.auto.yaml"
# Release times below are rendered as ET strings into the blackout YAML.

FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/current_year.asp"
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


def parse_fomc(html_text: str) -> list[RawCalendarEvent]:
    text = html_to_text(html_text)
    events: list[RawCalendarEvent] = []
    seen: set[dt.date] = set()
    for match in DATE_FULL_RE.finditer(text):
        year = int(match.group("year"))
        if year < dt.date.today().year - 1:
            continue
        month = match.group("month")
        day = match.group("endday") or match.group("day")
        event_date = parse_month_date(month, day, str(year))
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


BEA_TARGETS = {
    "GDP": "GDP",
    "Personal Income and Outlays": "PCE",
}


def parse_bea(html_text: str) -> list[RawCalendarEvent]:
    text = html_to_text(html_text)
    lines = text.splitlines()
    events: list[RawCalendarEvent] = []
    i = 0
    while i < len(lines):
        m = DATE_FULL_RE.fullmatch(lines[i])
        if not m:
            i += 1
            continue
        event_date = parse_month_date(m.group("month"), m.group("day"), m.group("year"))
        release_time = normalize_time(lines[i + 1]) if i + 1 < len(lines) and TIME_RE.fullmatch(lines[i + 1]) else None
        title_idx = i + 3 if i + 2 < len(lines) and lines[i + 2].lower() == "news" else i + 2
        title = lines[title_idx] if title_idx < len(lines) else ""
        for title_prefix, key in BEA_TARGETS.items():
            if title.startswith(title_prefix):
                events.append(RawCalendarEvent("bea", key, event_date, release_time))
                break
        i += 1
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


BLACKOUT_HEADER = """# Auto-generated by scripts/sync_macro_events.py\n#\n# This file is safe to overwrite. Put one-off manual overrides in\n# options.event_blackouts inside your main config instead of editing this file.\n#\n# Sources used by the sync script:\n# - Federal Reserve FOMC calendar\n# - BLS release calendar\n# - BEA release schedule\n# - Census advance indicators release schedule\n#\n# Generated at: {generated_at}\nevents:\n"""


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


def sync(days_ahead: int, include_census: bool = True) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    sources = [
        (FED_FOMC_URL, parse_fomc),
        (BLS_CALENDAR_URL, parse_bls),
        (BEA_SCHEDULE_URL, parse_bea),
    ]
    if include_census:
        sources.append((CENSUS_ADVANCE_URL, parse_census))
    for url, parser in sources:
        try:
            html_text = fetch_url(url)
            events = parser(html_text)
        except (URLError, HTTPError, TimeoutError, ValueError) as exc:
            warnings.append(f"{url}: {exc}")
            continue
        except Exception as exc:  # pragma: no cover - defensive runtime safety
            warnings.append(f"{url}: unexpected parser error: {exc}")
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
    payload = {"events": rows}
    rendered = yaml.safe_dump(payload, sort_keys=False)
    path.write_text(header + rendered[len("events:\n"):], encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync official macro calendars into the bot's blackout YAML format.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"Output YAML path. Default: {DEFAULT_OUTPUT} (repo root, next to config.yaml)")
    parser.add_argument("--days-ahead", type=int, default=90, help="Number of days ahead to include. Default: 90")
    parser.add_argument("--no-census", action="store_true", help="Skip the Census retail-sales source if you do not want that event family.")
    parser.add_argument("--stdout", action="store_true", help="Print the generated YAML to stdout instead of writing a file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, warnings = sync(days_ahead=max(1, args.days_ahead), include_census=not args.no_census)
    payload = {"events": rows}
    if args.stdout:
        sys.stdout.write(yaml.safe_dump(payload, sort_keys=False))
    else:
        out_path = Path(args.output)
        write_yaml(out_path, rows)
        sys.stderr.write(f"Wrote {len(rows)} blackout event(s) to {out_path}\n")
    if warnings:
        sys.stderr.write("Warnings:\n")
        for warning in warnings:
            sys.stderr.write(f"  - {warning}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
