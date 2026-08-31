"""
migrate_sqlite_to_neon.py

One-off: copy the local SQLite database (data/courses.db) into the Neon
Postgres database named by DATABASE_URL.

Why this exists instead of just scraping straight to Neon: UIUC's Course
Explorer WAF soft-blocks a full-catalog scrape after a handful of subjects -
it starts returning HTTP 200 with empty course lists rather than a clean 403,
so `scraper.py` run against DATABASE_URL only lands a few hundred sections
before everything else comes back empty (see DECISIONS.md). The local SQLite
file was built up over earlier residential-IP scrapes and *is* complete, so
the reliable path to a populated Neon database is to migrate that file.

What it does:
  * creates every table on Neon using the app's own schema-init functions
    (so the Postgres schema is identical to what the scraper/loaders would
    have made)
  * for each data table: DELETE all rows on Neon, then bulk-insert every row
    from SQLite (drop-and-reload - safe to re-run)
  * does NOT touch course_embeddings - run `python -m app.backfill_embeddings`
    afterwards to (re)build vectors from the migrated `sections` rows

Usage:
    python -m app.migrate_sqlite_to_neon
"""

import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db  # noqa: E402  - must follow load_dotenv so DATABASE_URL is seen
from app import embeddings  # noqa: E402
from app import scraper  # noqa: E402
from app import load_geneds, load_grades, load_tre  # noqa: E402
from app.db import DB_PATH  # noqa: E402

# Tables to copy, in a safe order (no FK constraints are enforced, but this
# reads naturally). course_embeddings is deliberately excluded.
TABLES = [
    "sections",
    "meetings",
    "gen_ed_categories",
    "grade_distributions",
    "teachers_ranked_excellent",
]


def sqlite_columns(sconn, table):
    """Column names on the SQLite table, minus the surrogate `id` primary key
    (Postgres reassigns its own via SERIAL)."""
    return [row[1] for row in sconn.execute(f"PRAGMA table_info({table})") if row[1] != "id"]


def bulk_reload(neon, table, columns, rows):
    """DELETE everything on the Neon table, then insert `rows` in one pass
    with execute_values (far faster than executemany's row-at-a-time)."""
    from psycopg2.extras import execute_values

    raw = neon._raw
    with raw.cursor() as cur:
        cur.execute(f"DELETE FROM {table}")
        if rows:
            col_list = ", ".join(columns)
            execute_values(
                cur,
                f"INSERT INTO {table} ({col_list}) VALUES %s",
                rows,
                page_size=1000,
            )
    raw.commit()
    return len(rows)


def main() -> int:
    if not DB_PATH.exists():
        print(f"No local SQLite database at {DB_PATH} - nothing to migrate.", flush=True)
        return 1

    # scraper.init_db() opens a connection via db.get_connection() (Postgres
    # because DATABASE_URL is set) and creates `sections` + `meetings`. Reuse
    # that same connection for the rest of the schema and the data copy.
    neon = scraper.init_db()
    if neon.backend != "postgres":
        print(
            "DATABASE_URL is not set (or not a Postgres URL) - this connected to "
            "SQLite. Set DATABASE_URL to your Neon URL and retry.",
            flush=True,
        )
        neon.close()
        return 1

    load_geneds.init_table(neon)
    load_grades.init_table(neon)
    load_tre.init_table(neon)
    embeddings.init_course_embeddings_table(neon)  # table exists; vectors filled later
    print("Neon schema ready (sections, meetings, gen_ed_categories, "
          "grade_distributions, teachers_ranked_excellent, course_embeddings).", flush=True)

    sconn = sqlite3.connect(DB_PATH)

    total = 0
    for table in TABLES:
        columns = sqlite_columns(sconn, table)
        rows = [tuple(r) for r in sconn.execute(f"SELECT {', '.join(columns)} FROM {table}")]
        n = bulk_reload(neon, table, columns, rows)
        total += n
        print(f"  {table}: {n} rows copied", flush=True)

    sconn.close()

    # Re-read straight from Neon as an independent check.
    print("\nNeon row counts after migration:", flush=True)
    for table in TABLES + ["course_embeddings"]:
        c = neon.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        print(f"  {table}: {c}", flush=True)
    neon.close()

    print(
        f"\nDone. {total} rows migrated. Next: `python -m app.backfill_embeddings` "
        "to build course_embeddings from the migrated sections.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
