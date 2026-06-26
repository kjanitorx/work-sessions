#!/usr/bin/env python3
"""
work-sessions: report working hours from macOS power management logs.

Data sources (tried in order for each day):
  1. /private/var/log/powermanagement/YYYY.MM.DD.asl  — daily archives, goes back weeks
  2. pmset -g log                                     — fallback for today / missing files

Background system cycles ("Sleep Service Back to Sleep", "Maintenance Sleep",
DarkWake) are ignored, as they run all night and don't mean the user is absent.
Sessions crossing midnight are split and shown on each day separately.
An ongoing session (display currently on) is marked with *.

2026-06-26,Fri,09:38-11:33*,1:54
2026-06-26,Fri,08:58-09:05,0:06
2026-06-25,Thu,14:13-18:20,4:06
2026-06-25,Thu,08:54-12:31,3:36
2026-06-24,Wed,13:48-18:14,4:25
2026-06-24,Wed,13:19-13:32,0:12
2026-06-24,Wed,09:00-12:58,3:57
2026-06-23,Tue,12:58-16:43,3:45
2026-06-23,Tue,09:30-12:30,2:59
2026-06-22,Mon,09:08-16:45,7:36
"""

import subprocess
import re
import sys
import calendar
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from pathlib import Path

ASL_DIR = Path("/private/var/log/powermanagement")

BACKGROUND_SLEEP = (
    "Sleep Service Back to Sleep",
    "Maintenance Sleep",
    "SleepService",
)

MONTH_ABBR: dict[str, int] = {v: k for k, v in enumerate(calendar.month_abbr) if v}

_ASL_PAT = re.compile(
    r"^(\w{3}\s+\d{1,2} \d{2}:\d{2}:\d{2})\s+\S+\s+powerd\[\d+\]\s+<\w+>:\s+(.*?)\s*$"
)
_PMSET_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\s+"
    r"(Notification|Wake|Sleep|DarkWake)\s{4,}"
    r"(.*?)\s*$"
)

type Event = tuple[datetime, str]
type RawSession = tuple[datetime, datetime, bool]


@dataclass
class Session:
    start: datetime
    end: datetime
    ongoing: bool

    def duration(self) -> timedelta:
        return self.end - self.start


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="macOS work session tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s                        current week Mon through today
  %(prog)s --days 7               last 7 days
  %(prog)s --week 2026-W22        specific ISO week
  %(prog)s --start 2026-06-01     from date to today
  %(prog)s --start 2026-06-01 --end 2026-06-07
  %(prog)s --totals               add a daily total line
  %(prog)s --gap 5                show breaks (only merge gaps < 5 min)
  %(prog)s --gap 30               treat lunch as part of one session

gap tuning (--gap MIN):
  5   only merge display flickers
  15  absorb short away-from-desk moments (default)
  30  treat lunch as part of one session
"""
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, metavar="N", help="last N days")
    g.add_argument("--week", metavar="YYYY-WNN", help="ISO week, e.g. 2026-W22")
    g.add_argument("--start", metavar="YYYY-MM-DD", help="start date (inclusive)")
    p.add_argument("--end", metavar="YYYY-MM-DD", help="end date (inclusive, with --start)")
    p.add_argument("--gap", type=int, default=15, metavar="MIN",
                   help="merge gaps shorter than N minutes (default: 15)")
    p.add_argument("--min", type=int, default=5, metavar="MIN",
                   help="hide sessions shorter than N minutes (default: 5)")
    p.add_argument("--totals", action="store_true", help="print a daily total line")
    p.add_argument("--verbose", action="store_true", help="print cleaned events before report")
    return p.parse_args()


def week_bounds(week_str: str) -> tuple[date, date]:
    year, w = week_str.split("-W")
    monday = datetime.strptime(f"{year}-W{int(w):02d}-1", "%G-W%V-%u").date()
    return monday, monday + timedelta(days=6)


def get_date_range(args: argparse.Namespace) -> tuple[date, date]:
    today = date.today()
    if args.week:
        return week_bounds(args.week)
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else today
        return start, end
    if args.days:
        return today - timedelta(days=args.days - 1), today
    return today - timedelta(days=today.weekday()), today


def _classify(msg: str) -> str | None:
    """Return 'on', 'off', or None for a powerd log message."""
    match msg:
        case s if "Display is turned on" in s:
            return "on"
        case s if "Display is turned off" in s:
            return "off"
        case s if (s.startswith("Wake from") or s.startswith("Wake [") or s.startswith("Wake ")) \
                and "DarkWake" not in s:
            return "on"
        case s if s.startswith("Entering Sleep") and not any(bg in s for bg in BACKGROUND_SLEEP):
            return "off"
        case _:
            return None


def parse_asl_file(asl_path: Path, target_date: date) -> list[Event]:
    """
    Read one daily ASL file via syslog -f and return (datetime, kind) pairs.
    syslog format: "Jun  5 10:08:15 hostname powerd[pid] <Level>: message"
    Timestamps are in local time (no tz offset in the log).
    """
    local_tz = datetime.now().astimezone().tzinfo
    result = subprocess.run(
        ["syslog", "-f", str(asl_path)],
        capture_output=True, text=True
    )
    events: list[Event] = []
    for line in result.stdout.splitlines():
        m = _ASL_PAT.match(line)
        if not m:
            continue
        ts_str, msg = m.group(1), m.group(2)
        # "Jun  5 10:08:15" — parse with the year from the filename
        parts = ts_str.split()
        month = MONTH_ABBR.get(parts[0])
        if not month:
            continue
        try:
            ts = datetime(
                target_date.year, month, int(parts[1]),
                *map(int, parts[2].split(":")),
                tzinfo=local_tz,
            )
        except (ValueError, IndexError):
            continue
        kind = _classify(msg)
        if kind:
            events.append((ts, kind))
    return events


def parse_pmset_fallback(start_date: date, end_date: date) -> list[Event]:
    """
    Read from pmset -g log for dates not covered by ASL files.
    Uses the tabular pmset format: YYYY-MM-DD HH:MM:SS ±HHMM  EventType    message
    """
    local_tz = datetime.now().astimezone().tzinfo
    result = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True)
    cutoff_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=local_tz)
    cutoff_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=local_tz)
    events: list[Event] = []
    for line in result.stdout.splitlines():
        m = _PMSET_PAT.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
        if ts < cutoff_start or ts > cutoff_end:
            continue
        kind_raw, msg = m.group(2), m.group(3)
        if kind_raw == "DarkWake":
            continue
        if kind_raw == "Notification":
            if "Display is turned on" in msg:
                events.append((ts, "on"))
            elif "Display is turned off" in msg:
                events.append((ts, "off"))
        elif kind_raw == "Wake":
            events.append((ts, "on"))
        elif kind_raw == "Sleep" and not any(bg in msg for bg in BACKGROUND_SLEEP):
            events.append((ts, "off"))
    return events


def collect_events(start_date: date, end_date: date) -> list[Event]:
    """Collect events from ASL files and pmset.

    ASL files can be archived before the day ends, leaving recent events only
    in pmset. Always query pmset for all dates so incomplete ASL files are
    supplemented. Duplicate events from both sources collapse in dedup().
    """
    events: list[Event] = []
    d = start_date
    while d <= end_date:
        asl = ASL_DIR / f"{d.year}.{d.month:02d}.{d.day:02d}.asl"
        if asl.exists():
            events.extend(parse_asl_file(asl, d))
        d += timedelta(days=1)

    events.extend(parse_pmset_fallback(start_date, end_date))

    return sorted(events)


def filter_noise(events: list[Event], min_off_seconds: int = 60) -> list[Event]:
    """Remove (off → on) pairs shorter than min_off_seconds (display flickers)."""
    result: list[Event] = []
    i = 0
    while i < len(events):
        ts, kind = events[i]
        if kind == "off" and i + 1 < len(events):
            next_ts, next_kind = events[i + 1]
            if next_kind == "on" and (next_ts - ts).total_seconds() < min_off_seconds:
                i += 2
                continue
        result.append((ts, kind))
        i += 1
    return result


def dedup(events: list[Event]) -> list[Event]:
    out: list[Event] = []
    last: str | None = None
    for ts, kind in events:
        if kind != last:
            out.append((ts, kind))
            last = kind
    return out


def build_sessions(events: list[Event], now: datetime) -> list[Session]:
    sessions: list[Session] = []
    start: datetime | None = None
    for ts, kind in events:
        if kind == "on" and start is None:
            start = ts
        elif kind == "off" and start is not None:
            sessions.append(Session(start, ts, False))
            start = None
    if start is not None:
        sessions.append(Session(start, now, True))
    return sessions


def split_at_midnight(sessions: list[Session]) -> list[Session]:
    result: list[Session] = []
    for session in sessions:
        if session.start.date() == session.end.date():
            result.append(session)
            continue
        current = session.start
        while current.date() < session.end.date():
            midnight = datetime(current.year, current.month, current.day,
                                tzinfo=current.tzinfo) + timedelta(days=1)
            result.append(Session(current, midnight, False))
            current = midnight
        result.append(Session(current, session.end, session.ongoing))
    return result


def merge_sessions(sessions: list[Session], gap: timedelta) -> list[Session]:
    """Merge same-day adjacent sessions separated by less than gap."""
    if not sessions:
        return []
    merged = [Session(sessions[0].start, sessions[0].end, sessions[0].ongoing)]
    for session in sessions[1:]:
        prev = merged[-1]
        if prev.start.date() == session.start.date() and session.start - prev.end < gap:
            prev.end = session.end
            prev.ongoing = session.ongoing
        else:
            merged.append(Session(session.start, session.end, session.ongoing))
    return merged


def fmt_duration(td: timedelta) -> str:
    total_min = max(0, int(td.total_seconds() / 60))
    return f"{total_min // 60}:{total_min % 60:02d}"


def main() -> None:
    args = parse_args()
    start_date, end_date = get_date_range(args)
    gap = timedelta(minutes=args.gap)
    min_duration = timedelta(minutes=args.min)
    now = datetime.now().astimezone()

    events = collect_events(start_date, end_date)
    if not events:
        print(
            f"No events found for {start_date} – {end_date}.\n"
            "Check that /private/var/log/powermanagement/ has files for this range.",
            file=sys.stderr,
        )
        sys.exit(1)

    events = filter_noise(events)
    events = dedup(events)

    if args.verbose:
        print("# Cleaned events:")
        for ts, kind in events:
            print(f"  {ts.strftime('%Y-%m-%d %H:%M:%S')}  {kind}")
        print()

    sessions = build_sessions(events, now)
    sessions = split_at_midnight(sessions)
    sessions = merge_sessions(sessions, gap)

    by_date: dict[date, list[Session]] = {}
    for session in sessions:
        by_date.setdefault(session.start.date(), []).append(session)

    if not by_date:
        print("No work sessions found for this period.", file=sys.stderr)
        sys.exit(1)

    for d in sorted(by_date.keys(), reverse=True):
        day_name = d.strftime("%a")
        day_sessions = sorted(
            (s for s in by_date[d] if s.duration() >= min_duration),
            key=lambda s: s.start,
            reverse=True,
        )
        if not day_sessions:
            continue
        for s in day_sessions:
            end_str = s.end.strftime("%H:%M") + ("*" if s.ongoing else "")
            print(f"{d},{day_name},{s.start.strftime('%H:%M')}-{end_str},{fmt_duration(s.duration())}")
        if args.totals:
            total = sum((s.duration() for s in day_sessions), timedelta())
            print(f"{d},{day_name},TOTAL,,{fmt_duration(total)}")


if __name__ == "__main__":
    main()
