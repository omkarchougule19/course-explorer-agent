"""
load_grades.py

Downloads the UIUC grade distribution dataset (wadefagen/datasets, official UIUC
data from Spring 2025 onward, FOIA-sourced before that) and loads it into a local
grade_distributions table, one row per (year, term, subject, course_number,
sched_type, primary_instructor).

Free, public CSV, no auth. https://github.com/wadefagen/datasets/blob/main/gpa/

Usage:
    python -m app.load_grades
"""

import csv
import io
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from app import db
from app.db import DB_PATH
from app.terms import ACTIVE_TERM_KEYS

# Load DATABASE_URL from the project-root .env so `python -m app.load_grades`
# targets Neon when it's configured, not just local SQLite.
load_dotenv(Path(__file__).parent.parent / ".env")

CSV_URL = "https://raw.githubusercontent.com/wadefagen/datasets/main/gpa/uiuc-gpa-dataset.csv"

GRADE_COLUMNS = (
    "a_plus", "a", "a_minus", "b_plus", "b", "b_minus",
    "c_plus", "c", "c_minus", "d_plus", "d", "d_minus", "f", "w",
)
CSV_GRADE_KEYS = (
    "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F", "W",
)


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS grade_distributions (
            id {db.autoincrement_pk()},
            year INTEGER NOT NULL,
            term TEXT NOT NULL,
            year_term TEXT,
            subject TEXT NOT NULL,
            course_number TEXT NOT NULL,
            course_title TEXT,
            sched_type TEXT,
            primary_instructor TEXT,
            a_plus INTEGER, a INTEGER, a_minus INTEGER,
            b_plus INTEGER, b INTEGER, b_minus INTEGER,
            c_plus INTEGER, c INTEGER, c_minus INTEGER,
            d_plus INTEGER, d INTEGER, d_minus INTEGER,
            f INTEGER, w INTEGER, students INTEGER,
            UNIQUE(year, term, subject, course_number, sched_type, primary_instructor)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grades_course "
        "ON grade_distributions(subject, course_number)"
    )
    conn.commit()


def _int_or_none(value: str):
    value = (value or "").strip()
    return int(value) if value else None


def fetch_rows():
    """Download the CSV and yield parsed row tuples ready for insertion, skipping
    anything outside the active term window (see app/terms.py) - this dataset goes
    back to 2010, but only the current term plus two terms either side is useful
    here. Raises on network failure - this is a manual/periodic script, not
    something the main scrape run depends on, so it's fine to let the caller decide
    how to handle that."""
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        year = _int_or_none(row.get("Year"))
        term = (row.get("Term") or "").strip().lower()
        if f"{year}-{term}" not in ACTIVE_TERM_KEYS:
            continue
        grades = [_int_or_none(row.get(k)) for k in CSV_GRADE_KEYS]
        yield (
            year,
            term,
            (row.get("YearTerm") or "").strip(),
            (row.get("Subject") or "").strip().upper(),
            (row.get("Number") or "").strip(),
            (row.get("Course Title") or "").strip() or None,
            (row.get("Sched Type") or "").strip() or None,
            (row.get("Primary Instructor") or "").strip() or None,
            *grades,
            _int_or_none(row.get("Students")),
        )


GRADE_ALL_COLUMNS = (
    "year", "term", "year_term", "subject", "course_number", "course_title",
    "sched_type", "primary_instructor", *GRADE_COLUMNS, "students",
)
GRADE_CONFLICT_COLUMNS = ("year", "term", "subject", "course_number", "sched_type", "primary_instructor")


def _prune_outside_window(conn: db.Connection) -> int:
    """Remove any rows left over from before the active term window was
    introduced (e.g. an earlier full-history load) that fall outside it now."""
    placeholders = ",".join(["?"] * len(ACTIVE_TERM_KEYS))
    cur = conn.execute(
        f"DELETE FROM grade_distributions WHERE (year || '-' || term) NOT IN ({placeholders})",
        list(ACTIVE_TERM_KEYS),
    )
    conn.commit()
    return cur.rowcount


def load(db_path=None) -> int:
    conn = db.get_connection(db_path)
    init_table(conn)

    rows = list(fetch_rows())
    if rows:
        db.upsert(conn, "grade_distributions", GRADE_ALL_COLUMNS, rows, GRADE_CONFLICT_COLUMNS)
        conn.commit()

    pruned = _prune_outside_window(conn)
    if pruned:
        print(f"Pruned {pruned} row(s) outside the active term window.", flush=True)
    conn.close()
    return len(rows)


if __name__ == "__main__":
    print(f"Downloading grade distribution dataset from {CSV_URL} ...", flush=True)
    try:
        n = load()
    except requests.RequestException as exc:
        print(f"Download failed: {exc}", flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f"Database error: {exc}", flush=True)
        sys.exit(1)
    print(f"Loaded {n} grade distribution rows into {DB_PATH}.", flush=True)
