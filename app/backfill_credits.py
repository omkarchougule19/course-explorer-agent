"""
backfill_credits.py

One-off (and safe to re-run): add the parsed credit columns to `sections`
if they're missing and fill them from each row's own credit_hours and
description (app/credits.py). No re-scrape; only the six credit columns are
written. New rows get them at save time (scraper.save_sections).

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


def add_missing_columns(conn: db.Connection) -> list:
    existing = db.existing_columns(conn, "sections")
    added = []
    for col, typ in CREDIT_COLUMN_TYPES:
        if col not in existing:
            conn.execute(f"ALTER TABLE sections ADD COLUMN {col} {typ}")
            added.append(col)
    return added


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
    finally:
        conn.close()


if __name__ == "__main__":
    main()
