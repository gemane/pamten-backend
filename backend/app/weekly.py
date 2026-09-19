"""Calendar weeks, the unit the activity digest counts in.

ISO weeks — Monday to Sunday, "2026-W38" — because a weekly report is compared
week over week, and rolling seven-day windows drift. Everything is UTC: the
counters are stamped in UTC, and a week boundary that moved with someone's
clock would count the same search in two weeks or none.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_WEEK_ID = re.compile(r"^(\d{4})-W(\d{2})$")


def iso_week(dt: datetime | None = None) -> str:
    """The ISO week id ("2026-W38") of a moment, now by default."""
    dt = dt or datetime.now(timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week_id: str) -> tuple[datetime, datetime]:
    """[Monday 00:00 UTC, next Monday 00:00 UTC) of a week id."""
    m = _WEEK_ID.match(week_id or "")
    if not m:
        raise ValueError(f"not a week id: {week_id!r}")
    year, week = int(m.group(1)), int(m.group(2))
    start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=7)


def previous_week(now: datetime | None = None) -> str:
    """The last COMPLETED week — what a Monday-morning digest reports on."""
    now = now or datetime.now(timezone.utc)
    return iso_week(now - timedelta(days=7))


def week_label(week_id: str) -> str:
    """"week 38 (14–20 Sep 2026)" — for a subject line or a heading."""
    start, end = week_bounds(week_id)
    last = end - timedelta(days=1)
    if start.month == last.month:
        span = f"{start.day}–{last.day} {last.strftime('%b %Y')}"
    else:
        span = f"{start.strftime('%d %b')} – {last.strftime('%d %b %Y')}"
    return f"week {int(week_id.split('-W')[1])} ({span})"
