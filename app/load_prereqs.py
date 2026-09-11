"""
load_prereqs.py

Parses the free-text `Prerequisite:` clause out of every course's
`sections.description` (via app/prereqs.py) and stores it as structured rows
in a `prerequisites` table.

No new external source - this only re-reads data the scraper already
fetched. Run it after a scrape (or a `load_catalog_snapshot`), whenever
descriptions change:

    python -m app.load_prereqs

Accuracy is best-effort (see app/prereqs.py). `raw_text` is stored on every
row so a caller can always fall back to quoting the original sentence.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from app import db
from app.db import DB_PATH
from app.prereqs import parse_prerequisites

# Target Neon when DATABASE_URL is set in the project-root .env, like the
# other loaders.
load_dotenv(Path(__file__).parent.parent / ".env")

PREREQ_COLUMNS = (
    "subject", "course_number", "group_index", "req_subject",
    "req_course_number", "relation", "condition_text", "raw_text",
)
PREREQ_CONFLICT_COLUMNS = (
    "subject", "course_number", "group_index", "req_subject",
    "req_course_number", "condition_text",
)


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS prerequisites (
            id {db.autoincrement_pk()},
            subject TEXT NOT NULL,
            course_number TEXT NOT NULL,
            group_index INTEGER NOT NULL,
            req_subject TEXT,
            req_course_number TEXT,
            relation TEXT NOT NULL DEFAULT 'prereq',
            condition_text TEXT,
            raw_text TEXT NOT NULL,
            UNIQUE(subject, course_number, group_index, req_subject,
                   req_course_number, condition_text)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prereq_course "
        "ON prerequisites(subject, course_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prereq_req "
        "ON prerequisites(req_subject, req_course_number)"
    )
    conn.commit()


def _rows_for(subject: str, course_number: str, description: str):
    """Yield prerequisite rows for one course. One row per OR-option course,
    one row per condition (course columns NULL)."""
    parsed = parse_prerequisites(description)
    if parsed.is_empty:
        return
    raw = parsed.raw
    gi = 0
    for group in parsed.groups:
        for (req_subj, req_num) in group.options:
            yield (subject, course_number, gi, req_subj, req_num,
                   group.relation, None, raw)
        for cond in group.conditions:
            yield (subject, course_number, gi, None, None,
                   group.relation, cond, raw)
        gi += 1
    for cond in parsed.conditions:
        yield (subject, course_number, gi, None, None, "prereq", cond, raw)
        gi += 1


def fetch_rows(conn: db.Connection):
    # One description per course - the longest one when sections disagree, so
    # a truncated variant doesn't spawn a second, thinner parse.
    rows = conn.execute(
        "SELECT subject, course_number, description FROM sections "
        "WHERE description IS NOT NULL AND description <> ''"
    ).fetchall()
    best: dict[tuple, str] = {}
    for r in rows:
        key = (r["subject"], r["course_number"])
        if len(r["description"]) > len(best.get(key, "")):
            best[key] = r["description"]
    for (subject, course_number), description in best.items():
        yield from _rows_for(subject, course_number, description)


def load(db_path=None) -> int:
    conn = db.get_connection(db_path)
    init_table(conn)
    rows = list(fetch_rows(conn))
    # Fully derived from descriptions - replace, don't accrete, so a course
    # that lost a prereq (or a parser change) doesn't leave stale rows behind.
    conn.execute("DELETE FROM prerequisites")
    if rows:
        db.upsert(conn, "prerequisites", PREREQ_COLUMNS, rows, PREREQ_CONFLICT_COLUMNS)
    conn.commit()
    conn.close()
    return len(rows)


if __name__ == "__main__":
    print("Parsing prerequisites from course descriptions ...", flush=True)
    try:
        n = load()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}", flush=True)
        sys.exit(1)
    print(f"Loaded {n} prerequisite rows into {DB_PATH}.", flush=True)
