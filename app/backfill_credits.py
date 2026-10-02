"""
backfill_credits.py

One-off (and safe to re-run): add the parsed columns if they're missing and
fill them from each row's own text - the credit columns on `sections`
(app/credits.py), instructor_last/instructor_initial/title_search on
`sections` (app/searchfields.py), and start_min/end_min/is_online on
`meetings` (app/timefields.py, app/searchfields.py).
No re-scrape; only those columns are written. New rows get them at save
time (scraper.save_sections).

    python -m app.backfill_credits --dry-run   # counts only, writes nothing
    python -m app.backfill_credits             # SQLite locally, or Neon if DATABASE_URL is set
"""

import argparse
import collections
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db  # noqa: E402
from app.credits import CREDIT_COLUMN_TYPES, CREDIT_COLUMNS, credit_fields  # noqa: E402
from app.timefields import MEETING_TIME_COLUMN_TYPES, meeting_minutes  # noqa: E402
from app.searchfields import (SEARCH_COLUMN_TYPES_MEETINGS, SEARCH_COLUMN_TYPES_SECTIONS,  # noqa: E402
                              is_online, section_search_fields)


def add_missing_columns(conn: db.Connection, table: str = "sections", types=CREDIT_COLUMN_TYPES) -> list:
    existing = db.existing_columns(conn, table)
    added = []
    for col, typ in types:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            added.append(f"{table}.{col}")
    return added


def backfill_section_search(conn: db.Connection, dry_run: bool) -> None:
    rows = conn.execute("SELECT id, instructor, course_label FROM sections").fetchall()
    updates = [(r["id"],) + section_search_fields(r["instructor"], r["course_label"]) for r in rows]
    print(f"section search fields: {len(updates)} ({sum(1 for u in updates if u[1])} with an "
          f"instructor last name, {sum(1 for u in updates if u[3])} with a title)")
    if dry_run:
        return
    print(f"columns added: {add_missing_columns(conn, 'sections', SEARCH_COLUMN_TYPES_SECTIONS) or 'none'}")
    if conn.backend == "postgres":
        from psycopg2.extras import execute_values
        execute_values(
            conn._raw.cursor(),
            "UPDATE sections AS s SET instructor_last = v.instructor_last, "
            "instructor_initial = v.instructor_initial, title_search = v.title_search "
            "FROM (VALUES %s) AS v(id, instructor_last, instructor_initial, title_search) WHERE s.id = v.id",
            updates, page_size=1000)
    else:
        conn.executemany("UPDATE sections SET instructor_last = ?, instructor_initial = ?, "
                         "title_search = ? WHERE id = ?", [u[1:] + (u[0],) for u in updates])
    conn.commit()
    print(f"updated {len(updates)} sections (search fields)")


def backfill_meetings(conn: db.Connection, dry_run: bool) -> None:
    rows = conn.execute("SELECT id, start_time, end_time, meeting_type FROM meetings").fetchall()
    updates = [(r["id"],) + meeting_minutes(r["start_time"], r["end_time"])
               + (is_online(r["meeting_type"]),) for r in rows]
    timed = sum(1 for u in updates if u[1] is not None)
    print(f"meetings: {len(updates)} ({timed} with a parsed start time, "
          f"{len(updates) - timed} ARRANGED/blank)")
    if dry_run:
        return
    print(f"columns added: "
          f"{add_missing_columns(conn, 'meetings', MEETING_TIME_COLUMN_TYPES + SEARCH_COLUMN_TYPES_MEETINGS) or 'none'}")
    if conn.backend == "postgres":
        from psycopg2.extras import execute_values
        execute_values(
            conn._raw.cursor(),
            "UPDATE meetings AS m SET start_min = v.start_min::integer, end_min = v.end_min::integer, "
            "is_online = v.is_online::integer "
            "FROM (VALUES %s) AS v(id, start_min, end_min, is_online) WHERE m.id = v.id",
            updates, page_size=1000)
    else:
        conn.executemany("UPDATE meetings SET start_min = ?, end_min = ?, is_online = ? WHERE id = ?",
                         [u[1:] + (u[0],) for u in updates])
    conn.commit()
    print(f"updated {len(updates)} meetings")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        print(f"backend: {conn.backend}")
        rows = conn.execute("SELECT id, course_number, credit_hours, description FROM sections").fetchall()
        updates = [(r["id"],) + credit_fields(r["course_number"], r["credit_hours"], r["description"])
                   for r in rows]

        stats = collections.Counter()
        for u in updates:
            _, lo, hi, grad, _, _, restr = u
            stats["hours parsed" if lo is not None else "hours unparsed"] += 1
            stats[f"grad_credit={grad}"] += 1
            if restr:
                stats["restriction"] += 1
        print(f"sections: {len(updates)}")
        for k, v in sorted(stats.items()):
            print(f"  {k:<22} {v}")
        if args.dry_run:
            backfill_section_search(conn, dry_run=True)
            backfill_meetings(conn, dry_run=True)
            print("dry run: nothing written")
            return

        added = add_missing_columns(conn)
        print(f"columns added: {added or 'none (already there)'}")
        sets = ", ".join(f"{c} = ?" for c in CREDIT_COLUMNS)
        params = [u[1:] + (u[0],) for u in updates]
        if conn.backend == "postgres":
            # One batched UPDATE ... FROM (VALUES ...) per page instead of
            # ~20K round trips to Neon.
            from psycopg2.extras import execute_values
            cur = conn._raw.cursor()
            execute_values(
                cur,
                "UPDATE sections AS s SET credit_min = v.credit_min::real, credit_max = v.credit_max::real, "
                "grad_credit = v.grad_credit, grad_min = v.grad_min::real, grad_max = v.grad_max::real, "
                "restriction = v.restriction "
                "FROM (VALUES %s) AS v(id, credit_min, credit_max, grad_credit, grad_min, grad_max, restriction) "
                "WHERE s.id = v.id",
                [(u[0],) + u[1:] for u in updates],
                page_size=1000,
            )
        else:
            conn.executemany(f"UPDATE sections SET {sets} WHERE id = ?", params)
        conn.commit()
        print(f"updated {len(updates)} sections")
        backfill_section_search(conn, dry_run=False)
        backfill_meetings(conn, dry_run=False)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
