"""
load_geneds.py

Downloads UIUC's Gen Ed course categorization dataset (wadefagen/datasets) and
loads it into a local gen_ed_categories table.

Free, public CSV, no auth. https://github.com/wadefagen/datasets/tree/main/geneds

Caveat: unlike the grade and TRE datasets, this one is a single point-in-time
snapshot (currently Spring 2023), not refreshed every term. Gen Ed
classifications don't change often once a course has one, so it's still useful
as a "best known" categorization, but it's joined to `sections` by
(subject, course_number) only - not scoped to a specific year/term - and a
course added or reclassified after the snapshot won't show up correctly.

Usage:
    python -m app.load_geneds
"""

import csv
import io
import re
import sys

import requests

from app import db
from app.db import DB_PATH

CSV_URL = "https://raw.githubusercontent.com/wadefagen/datasets/main/geneds/gened-courses.csv"

CATEGORY_COLUMNS = ("acp", "cs", "hum", "nat", "qr", "sbs")
CSV_CATEGORY_KEYS = ("ACP", "CS", "HUM", "NAT", "QR", "SBS")

COURSE_RE = re.compile(r"^(\S+)\s+(\S+)$")


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS gen_ed_categories (
            id {db.autoincrement_pk()},
            snapshot_year INTEGER,
            snapshot_term TEXT,
            subject TEXT NOT NULL,
            course_number TEXT NOT NULL,
            course_title TEXT,
            acp TEXT, cs TEXT, hum TEXT, nat TEXT, qr TEXT, sbs TEXT,
            UNIQUE(subject, course_number)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_geneds_course ON gen_ed_categories(subject, course_number)"
    )
    conn.commit()


def fetch_rows():
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        match = COURSE_RE.match((row.get("Course") or "").strip())
        if not match:
            continue
        subject, course_number = match.groups()
        categories = [(row.get(k) or "").strip() or None for k in CSV_CATEGORY_KEYS]
        year_raw = (row.get("Year") or "").strip()
        yield (
            int(year_raw) if year_raw.isdigit() else None,
            (row.get("Term") or "").strip().lower() or None,
            subject.upper(),
            course_number,
            (row.get("Course Title") or "").strip() or None,
            *categories,
        )


GENED_COLUMNS = ("snapshot_year", "snapshot_term", "subject", "course_number", "course_title", *CATEGORY_COLUMNS)
GENED_CONFLICT_COLUMNS = ("subject", "course_number")


def load(db_path=None) -> int:
    conn = db.get_connection(db_path)
    init_table(conn)

    rows = list(fetch_rows())
    if not rows:
        conn.close()
        return 0

    db.upsert(conn, "gen_ed_categories", GENED_COLUMNS, rows, GENED_CONFLICT_COLUMNS)
    conn.commit()
    conn.close()
    return len(rows)


if __name__ == "__main__":
    print(f"Downloading Gen Ed categories dataset from {CSV_URL} ...", flush=True)
    try:
        n = load()
    except requests.RequestException as exc:
        print(f"Download failed: {exc}", flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f"Database error: {exc}", flush=True)
        sys.exit(1)
    print(f"Loaded {n} gen-ed category rows into {DB_PATH}.", flush=True)
