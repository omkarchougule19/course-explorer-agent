"""
load_calendar.py

Loads one term of UIUC's registrar academic calendar (instruction dates,
add/drop/withdraw deadlines, breaks, holidays, finals, grade deadlines) into
an `academic_calendar` table.

The registrar publishes one HTML page per term as a definition list
(`<dt>` date / `<dd>` event), and the per-term URL is NOT uniformly
derivable (`/fall-2026-academic-calendar/` vs. archived paths), so the URL
(or a saved file) is passed explicitly:

    python -m app.load_calendar --term 2026-fall \\
        --url https://registrar.illinois.edu/fall-2026-academic-calendar/

    python -m app.load_calendar --term 2026-fall --file saved_calendar.html

Parsing is text-oriented (not tied to exact tags) so a markup change on the
registrar side is survivable: a fragment that reads like a date becomes the
current date, and the fragments after it are its events. `raw_date` and
`title` are always stored, so an event we can't categorise is still
queryable.
"""

import argparse
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import requests
from dotenv import load_dotenv

from app import db
from app.db import DB_PATH

load_dotenv(Path(__file__).parent.parent / ".env")

CALENDAR_COLUMNS = (
    "year", "semester", "event_date", "event_end_date", "title", "category",
    "raw_date", "source_url",
)
CALENDAR_CONFLICT_COLUMNS = ("year", "semester", "event_date", "title")

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
for _m, _i in list(_MONTHS.items()):
    _MONTHS[_m[:3]] = _i  # jan, feb, ...

_DATE_RE = re.compile(
    r"^\s*(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d1>\d{1,2})"
    r"(?:\s*[–—-]\s*(?:(?P<mon2>[A-Za-z]{3,9})\.?\s+)?(?P<d2>\d{1,2}))?\s*$"
)

# Some registrar events are split across two adjacent block elements - a
# title line, then a separate "Month Day at H:MM PM" line giving the exact
# time. That second fragment doesn't match _DATE_RE (it has a trailing "at
# <time>", so it isn't a bare date), and on its own it isn't a real event
# either - it's a continuation of the title fragment right before it. This
# catches that shape so it gets merged into the previous title instead of
# becoming its own bogus "October 13 at 2:00 PM" row (see DECISIONS.md).
_DATE_TIME_ONLY_RE = re.compile(
    r"^\s*[A-Za-z]{3,9}\.?\s+\d{1,2}\s+at\s+\d{1,2}(:\d{2})?\s*[AaPp]\.?[Mm]\.?\s*$"
)

# title keyword -> category, first match wins (order matters).
_CATEGORY_RULES = [
    (r"final exam|final examination", "finals"),
    (r"reading day", "instruction"),
    (r"instruction begins|last (instruction|day of instruction)|classes begin", "instruction"),
    (r"add deadline|add/drop", "add"),
    (r"drop deadline|drop without|10th day|last day to drop", "drop"),
    (r"withdraw", "withdraw"),
    (r"break", "break"),
    (r"no classes|holiday|labor day|thanksgiving|memorial day|spring recess", "holiday"),
    (r"grade (entry|deadline|reentry)|grade deadline|midterm grade", "grades"),
    (r"registration|time ticket|priority registration", "registration"),
    (r"conferral|commencement|degree application", "commencement"),
]


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS academic_calendar (
            id {db.autoincrement_pk()},
            year INTEGER NOT NULL,
            semester TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_end_date TEXT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            raw_date TEXT NOT NULL,
            source_url TEXT,
            loaded_at {db.current_timestamp_default()},
            UNIQUE(year, semester, event_date, title)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_term "
        "ON academic_calendar(year, semester, category)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_date "
        "ON academic_calendar(year, semester, event_date)"
    )
    conn.commit()


class _Text(HTMLParser):
    """Flatten HTML to newline-separated text fragments (one per block).

    Skips text inside <script>/<style> - their content is code/JSON, never
    page text, and without this a page's speculation-rules or emoji-data
    <script> blob gets read as if it were calendar text (see DECISIONS.md's
    "calendar page fixes" entry - a literal '{"prefetch":[...' JSON string
    was showing up as a bogus event before this)."""
    _BLOCK = {"dt", "dd", "li", "p", "br", "tr", "div", "h1", "h2", "h3", "h4"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def _flush(self):
        s = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if s:
            self.parts.append(s)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip_depth:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _term_year(month: int, term_year: int, semester: str) -> int:
    """A January/February date printed on a fall calendar belongs to the
    next calendar year."""
    if semester == "fall" and month <= 2:
        return term_year + 1
    return term_year


def _parse_date(fragment: str, term_year: int, semester: str):
    """('YYYY-MM-DD', 'YYYY-MM-DD' | None, matched_raw) or None."""
    m = _DATE_RE.match(fragment)
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon").lower())
    if not mon:
        return None
    d1 = int(m.group("d1"))
    y1 = _term_year(mon, term_year, semester)
    try:
        start = date(y1, mon, d1)
    except ValueError:
        return None
    end = None
    if m.group("d2"):
        mon2 = _MONTHS.get((m.group("mon2") or "").lower(), mon)
        y2 = _term_year(mon2, term_year, semester)
        try:
            end = date(y2, mon2, int(m.group("d2"))).isoformat()
        except ValueError:
            end = None
    return start.isoformat(), end, fragment.strip()


def _categorize(title: str) -> str:
    low = title.lower()
    for pattern, cat in _CATEGORY_RULES:
        if re.search(pattern, low):
            return cat
    return "other"



# Marks the end of the registrar page's actual article body (WordPress
# renders this literal comment right after the real .entry-content div
# closes). Everything past it is site chrome - the footer's address,
# office hours, and nav links - which used to get swept in as if it were
# more calendar text and attributed to whichever date heading happened to
# be last, producing nonsense entries like "901 West Illinois Street"
# under the final real date (see DECISIONS.md). Truncating here is a
# no-op (keeps the full page) if the site ever drops this comment.
_CONTENT_END_MARKER = "<!-- .entry-content -->"


def extract_events(html: str, term_year: int, semester: str, source_url: str | None):
    end = html.find(_CONTENT_END_MARKER)
    if end != -1:
        html = html[:end]

    parser = _Text()
    parser.feed(html)
    parser.close()

    rows = []
    cur = None  # (start, end, raw_date)
    for frag in parser.parts:
        parsed = _parse_date(frag, term_year, semester)
        if parsed:
            cur = parsed
            continue
        if cur is None or len(frag) < 4:
            continue
        if _DATE_TIME_ONLY_RE.match(frag) and rows and rows[-1][2] == cur[0]:
            prev = rows[-1]
            merged_title = (prev[4] + " - " + frag.strip(" ."))[:300]
            rows[-1] = prev[:4] + (merged_title, _categorize(merged_title)) + prev[6:]
            continue
        title = frag.strip(" .")[:300]
        rows.append((
            term_year, semester, cur[0], cur[1], title,
            _categorize(title), cur[2], source_url,
        ))
    # de-dup on the table's unique key
    seen = set()
    uniq = []
    for r in rows:
        k = (r[0], r[1], r[2], r[4])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def load(term: str, url: str | None = None, file: str | None = None, db_path=None) -> int:
    term_year_s, semester = term.split("-", 1)
    term_year, semester = int(term_year_s), semester.strip().lower()

    if file:
        html = Path(file).read_text(encoding="utf-8", errors="replace")
        source_url = None
    elif url:
        resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html, source_url = resp.text, url
    else:
        raise ValueError("pass --url or --file")

    rows = extract_events(html, term_year, semester, source_url)
    conn = db.get_connection(db_path)
    init_table(conn)
    # Replace this term's events - a re-scrape reflects the registrar's
    # current calendar, including any date that moved.
    conn.execute("DELETE FROM academic_calendar WHERE year = ? AND semester = ?",
                 (term_year, semester))
    if rows:
        db.upsert(conn, "academic_calendar", CALENDAR_COLUMNS, rows, CALENDAR_CONFLICT_COLUMNS)
    conn.commit()
    conn.close()
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Load one term of the UIUC academic calendar.")
    ap.add_argument("--term", required=True, help="e.g. 2026-fall")
    ap.add_argument("--url", help="registrar calendar page for that term")
    ap.add_argument("--file", help="a saved copy of that page instead of fetching")
    args = ap.parse_args()
    try:
        n = load(args.term, url=args.url, file=args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}", flush=True)
        sys.exit(1)
    target = "Neon Postgres (DATABASE_URL)" if db.is_postgres() else str(DB_PATH)
    print(f"Loaded {n} calendar events for {args.term} into {target}.", flush=True)
