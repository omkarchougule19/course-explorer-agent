"""
load_tre.py

Downloads UIUC's "Teachers Ranked as Excellent by their Students" dataset
(wadefagen/datasets, public records, back to Fall 2003) and loads it into a
local teachers_ranked_excellent table.

Free, public CSV, no auth. https://github.com/wadefagen/datasets/tree/main/teachers-ranked-as-excellent

Caveat: the source CSV has no subject code, only a department "unit" name
(e.g. "Computer Science") and a bare course number (e.g. "225"). There's no
reliable 1:1 mapping from unit name to our `subject` codes (a unit can span
multiple subjects, and naming doesn't always match), so this is stored as-is
and joined to `sections` best-effort (by course_number + a fuzzy match on
unit vs. course_label), not as a strict foreign key.

Usage:
    python -m app.load_tre
"""

import csv
import io
import re
import sys

import requests

from app import db
from app.db import DB_PATH
from app.terms import ACTIVE_TERM_KEYS

CSV_URL = "https://raw.githubusercontent.com/wadefagen/datasets/main/teachers-ranked-as-excellent/uiuc-tre-dataset.csv"

TERM_PREFIXES = {"fa": "fall", "sp": "spring", "su": "summer", "wi": "winter"}
TERM_RE = re.compile(r"^(fa|sp|su|wi)(\d{4})$")


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS teachers_ranked_excellent (
            id {db.autoincrement_pk()},
            year INTEGER NOT NULL,
            term TEXT NOT NULL,
            unit TEXT,
            last_name TEXT,
            first_name TEXT,
            role TEXT,
            ranking TEXT,
            course_number TEXT,
            UNIQUE(year, term, unit, last_name, first_name, role, course_number)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tre_name ON teachers_ranked_excellent(last_name, first_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tre_course ON teachers_ranked_excellent(course_number)"
    )
    conn.commit()


def _parse_term(raw: str):
    """'fa2003' -> (2003, 'fall'). Returns (None, None) if it doesn't match the
    expected pattern, so a format change upstream skips the row instead of crashing."""
    match = TERM_RE.match((raw or "").strip().lower())
    if not match:
        return None, None
    prefix, year = match.groups()
    return int(year), TERM_PREFIXES[prefix]


def fetch_rows():
    """Yields rows within the active term window only (see app/terms.py) - this
    dataset goes back to Fall 2003, but only the current term plus two terms
    either side is useful here."""
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        year, term = _parse_term(row.get("term"))
        if year is None or f"{year}-{term}" not in ACTIVE_TERM_KEYS:
            continue
        yield (
            year,
            term,
            (row.get("unit") or "").strip() or None,
            (row.get("lname") or "").strip() or None,
            (row.get("fname") or "").strip() or None,
            (row.get("role") or "").strip() or None,
            (row.get("ranking") or "").strip() or None,
            (row.get("course") or "").strip() or None,
        )


TRE_COLUMNS = ("year", "term", "unit", "last_name", "first_name", "role", "ranking", "course_number")
TRE_CONFLICT_COLUMNS = ("year", "term", "unit", "last_name", "first_name", "role", "course_number")


def _prune_outside_window(conn: db.Connection) -> int:
    """Remove any rows left over from before the active term window was
    introduced (e.g. an earlier full-history load) that fall outside it now."""
    placeholders = ",".join(["?"] * len(ACTIVE_TERM_KEYS))
    cur = conn.execute(
        f"DELETE FROM teachers_ranked_excellent WHERE (year || '-' || term) NOT IN ({placeholders})",
        list(ACTIVE_TERM_KEYS),
    )
    conn.commit()
    return cur.rowcount


def load(db_path=None) -> int:
    conn = db.get_connection(db_path)
    init_table(conn)

    rows = list(fetch_rows())
    if rows:
        db.upsert(conn, "teachers_ranked_excellent", TRE_COLUMNS, rows, TRE_CONFLICT_COLUMNS)
        conn.commit()

    pruned = _prune_outside_window(conn)
    if pruned:
        print(f"Pruned {pruned} row(s) outside the active term window.", flush=True)
    conn.close()
    return len(rows)


if __name__ == "__main__":
    print(f"Downloading Teachers Ranked as Excellent dataset from {CSV_URL} ...", flush=True)
    try:
        n = load()
    except requests.RequestException as exc:
        print(f"Download failed: {exc}", flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f"Database error: {exc}", flush=True)
        sys.exit(1)
    print(f"Loaded {n} teachers-ranked-excellent rows into {DB_PATH}.", flush=True)
