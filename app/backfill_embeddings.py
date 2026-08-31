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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="process at most N courses this run (for splitting a large backfill "
        "into chunks that each finish quickly); re-run to continue",
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

    remaining_after = 0
    if args.limit is not None and args.limit < len(courses):
        remaining_after = len(courses) - args.limit
        courses = courses[: args.limit]

    print(
        f"Embedding {len(courses)} course descriptions with {emb.MODEL_NAME} "
        f"(first run downloads the model, ~130MB) ...",
        flush=True,
    )
    emb.warmup()

    # Batched, not one-row-at-a-time: embed BATCH descriptions in a single
    # fastembed call, then upsert the whole batch with execute_values and one
    # commit. save_course_embedding() commits per row, which over a Neon
    # network connection turns a few thousand rows into a 30-40 min crawl (and
    # a long-running job the harness eventually kills). One commit per 200 rows
    # brings it down to a couple of minutes and makes an interrupted run cheap
    # to resume - it just re-skips whatever already landed.
    from pgvector import Vector
    from pgvector.psycopg2 import register_vector

    register_vector(conn._raw)
    BATCH = 200
    UPSERT = (
        "INSERT INTO course_embeddings (subject, course_number, description, embedding) "
        "VALUES %s "
        "ON CONFLICT (subject, course_number) DO UPDATE SET "
        "description = EXCLUDED.description, embedding = EXCLUDED.embedding, updated_at = now()"
    )

    # HNSW does index maintenance on every insert, which dominates the runtime
    # of a few-thousand-row load (~95s per 200-row batch with the index in
    # place). Standard pgvector bulk-load pattern: drop the vector index, load,
    # rebuild it once at the end. The index name matches
    # embeddings.init_course_embeddings_table().
    print("Dropping HNSW index for the bulk load ...", flush=True)
    with conn._raw.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_course_embeddings_vec")
    conn._raw.commit()
    rebuild_index = remaining_after == 0  # only the final chunk rebuilds it

    embedded = 0
    skipped = 0
    try:
        for start in tqdm(range(0, len(courses), BATCH), unit="batch"):
            chunk = courses[start:start + BATCH]
            vectors = emb.embed_texts([c[2] for c in chunk])
            rows = [
                (subject, course_number, description, Vector(vec))
                for (subject, course_number, description), vec in zip(chunk, vectors)
                if vec is not None
            ]
            skipped += len(chunk) - len(rows)
            if rows:
                with conn._raw.cursor() as cur:
                    from psycopg2.extras import execute_values
                    execute_values(cur, UPSERT, rows, page_size=BATCH)
                conn._raw.commit()
                embedded += len(rows)
    finally:
        # Rebuild the index once the backfill is actually complete (this run
        # had no --limit remainder, or the loop finished it). A missing index
        # would silently make course_content_search do a full scan on every
        # query, so if this run was interrupted mid-chunk, rebuild anyway
        # rather than leave it dropped.
        if rebuild_index or embedded + skipped >= len(courses):
            print("Rebuilding HNSW index ...", flush=True)
            with conn._raw.cursor() as cur:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_course_embeddings_vec "
                    "ON course_embeddings USING hnsw (embedding vector_cosine_ops)"
                )
            conn._raw.commit()
        else:
            print("Index left dropped - more chunks remain. Re-run without "
                  "--limit (or with a final --limit) to finish and rebuild it.",
                  flush=True)

    conn.close()
    tail = f" ~{remaining_after} still to do - re-run to continue." if remaining_after else ""
    print(f"Done. {embedded} embedded/updated, {skipped} skipped (no vector).{tail}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
