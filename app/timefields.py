"""
timefields.py

Meeting times as numbers: meetings.start_time / end_time are text in mixed
shapes ('09:00AM', '09:00 AM', 'ARRANGED'), so every time-of-day question
made the model copy a four-line CAST(substr(...)) expression into its SQL -
and it kept unbalancing the parentheses ("Which MATH 241 discussion sections
are in the afternoon?" hit the iteration cap on parse errors). start_min /
end_min hold minutes after midnight (NULL for ARRANGED or unparseable), so
"afternoon" is just start_min >= 720.

Used by scraper.save_sections for new rows and backfill_credits.py for old
ones. Pure functions, no I/O.
"""

import re
from typing import Optional

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s*$", re.IGNORECASE)

MEETING_TIME_COLUMN_TYPES = (("start_min", "INTEGER"), ("end_min", "INTEGER"))
MEETING_TIME_COLUMNS = tuple(c for c, _ in MEETING_TIME_COLUMN_TYPES)


def minutes_after_midnight(raw: Optional[str]) -> Optional[int]:
    """'09:30AM' / '09:30 AM' -> 570, '12:15PM' -> 735, '12:00AM' -> 0;
    None for 'ARRANGED', empty or anything else."""
    m = _TIME_RE.match(raw or "")
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        return None
    return (hour % 12) * 60 + minute + (720 if half == "P" else 0)


def meeting_minutes(start_time: Optional[str], end_time: Optional[str]) -> tuple:
    """Values for MEETING_TIME_COLUMNS, in order."""
    return minutes_after_midnight(start_time), minutes_after_midnight(end_time)
