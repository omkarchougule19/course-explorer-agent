"""
backfill_embeddings.py

One-off / catch-up: populate the `course_embeddings` pgvector table for
courses that are already in `sections` but have no embedding yet.

The live scraper (app/scraper.py) embeds each course's description as it
saves sections, so after a normal full scrape this script has nothing to do.
Use it when sections got into the database some other way - e.g.
`load_catalog_snapshot.py` backfilling an archived term, or a `--fast` scrape
that skipped descriptions, or pointing the app at a fresh Neon database and
adding pgvector after the fact.

Postgres/Neon only. `course_embeddings` is a pgvector table and does not
exist on the local SQLite fallback (see embeddings.py), so this exits early
with a message if `DATABASE_URL` isn't a Postgres URL.

Usage:
    # DATABASE_URL must point at Neon/Postgres (via .env or the environment)
    python -m app.backfill_embeddings
    python -m app.backfill_embeddings --force   # re-embed even courses that already have a row
"""

import argparse
import sys

from dotenv import load_dotenv
from tqdm import tqdm

from app import db
from app import embeddings as emb

load_dotenv()


def distinct_courses(conn):
    """Every (subject, course_number, description) with a usable description,
    collapsed to one row per course.

    `sections` has one row per *section*; the catalog description is identical
    across all sections of a course, so DISTINCT folds them together (matching
    how the scraper embeds once per course, not once per section). Rows with a
    NULL or blank description are dropped - there is nothing to embed."""
    rows = conn.execute(
        """
        SELECT DISTINCT subject, course_number, description
        FROM sections
        WHERE description IS NOT NULL AND TRIM(description) <> ''
        """
    ).fetchall()
    return [(r["subject"], r["course_number"], r["description"]) for r in rows]


def already_embedded(conn):
    """(subject, course_number) pairs that already have a vector, so a normal
    (non --force) run can skip them and only fill the gaps."""
    rows = conn.execute(
        "SELECT subject, course_number FROM course_embeddings"
    ).fetchall()
    return {(r["subject"], r["course_number"]) for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill course_embeddings from existing sections rows."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-embed every course, including ones that already have a row",
    )
    args = parser.parse_args()

    conn = db.get_connection()
    if conn.backend != "postgres":
        print(
            "Not a Postgres connection - course_embeddings only exists on "
            "Neon/pgvector. Set DATABASE_URL to your Neon URL and retry.",
            flush=True,
        )
        conn.close()
        return 1

    # Extension + table + HNSW index. Idempotent - safe to call on a database
    # that already has them (same call the scraper makes at startup).
    emb.init_course_embeddings_table(conn)

    courses = distinct_courses(conn)
    if not args.force:
        done = already_embedded(conn)
        courses = [c for c in courses if (c[0], c[1]) not in done]

    if not courses:
        print(
            "Nothing to backfill - every course with a description already has "
            "an embedding. Pass --force to rebuild them all.",
            flush=True,
        )
        conn.close()
        return 0

    print(
        f"Embedding {len(courses)} course descriptions with {emb.MODEL_NAME} "
        f"(first run downloads the model, ~130MB) ...",
        flush=True,
    )
    emb.warmup()

    embedded = 0
    failed = 0
    # save_course_embedding() embeds one description and upserts + commits it.
    # Per-row commits mean one network round-trip per course to Neon - fine for
    # a one-off catch-up of a few thousand rows, not something to run in a hot
    # path. It returns False (counted as "skipped") if there's no vector to
    # store, e.g. a description that's technically non-blank but unembeddable.
    for subject, course_number, description in tqdm(courses, unit="course"):
        try:
            if emb.save_course_embedding(conn, subject, course_number, description):
                embedded += 1
        except Exception as exc:  # noqa: BLE001 - one bad row shouldn't kill a long backfill
            failed += 1
            tqdm.write(f"  [warn] {subject} {course_number}: {exc}")

    conn.close()
    skipped = len(courses) - embedded - failed
    print(
        f"Done. {embedded} embedded, {failed} failed, {skipped} skipped.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
