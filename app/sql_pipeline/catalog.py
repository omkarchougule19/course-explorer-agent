"""
catalog.py

The real database schema, read live from whichever backend is active
(SQLite locally, Postgres/Neon in production - see app/db.py). The Critic's
static check (critique.py) compares every table/column a generated query
references against this, so a hallucinated `sections.professor` or
`FROM course_ratings` is caught before the query ever runs.

Only the tables the agent is allowed to touch are catalogued - the same
INCLUDED_TABLES list app/agent.py points the SQL toolkit at.
"""

from functools import lru_cache

from app import db
from app.agent import INCLUDED_TABLES


def _columns_for(conn: db.Connection, table: str) -> set[str]:
    """Column names on `table`, lowercased. Reuses db.existing_columns, which
    already abstracts PRAGMA table_info (SQLite) vs information_schema
    (Postgres)."""
    return {c.lower() for c in db.existing_columns(conn, table)}


def load_catalog() -> dict[str, set[str]]:
    """{table_name: {column, ...}} for every INCLUDED_TABLES entry that
    actually exists in the live database. A table missing entirely (e.g. a
    load script hasn't run) is simply left out - the Critic then flags any
    query that references it, which is the correct outcome."""
    conn = db.get_connection()
    try:
        catalog: dict[str, set[str]] = {}
        for table in INCLUDED_TABLES:
            cols = _columns_for(conn, table)
            if cols:
                catalog[table.lower()] = cols
        return catalog
    finally:
        conn.close()


@lru_cache(maxsize=1)
def get_terms() -> tuple[str, ...]:
    """The (semester, year) pairs actually present in `sections`, newest
    first, rendered as 'fall 2026'. The Generator needs this: an LLM asked
    about "this fall" otherwise guesses a year (often its training-cutoff
    year), which silently returns nothing against a 2026-only snapshot."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT year, semester FROM sections "
            "WHERE year IS NOT NULL AND semester IS NOT NULL "
            "ORDER BY year DESC, semester"
        ).fetchall()
    except Exception:
        return ()
    finally:
        conn.close()
    return tuple(f"{r['semester']} {r['year']}" for r in rows)


def terms_note() -> str:
    terms = get_terms()
    if not terms:
        return ""
    return (
        "Terms present in the data (there is no other year): "
        + ", ".join(terms)
        + ". Interpret 'this'/'current'/'upcoming' semester as the most recent of these."
    )


@lru_cache(maxsize=1)
def get_catalog() -> dict[str, frozenset[str]]:
    """Process-cached catalog. The schema doesn't change within a run, and
    re-reading it per question would add a round-trip to Neon each time.
    Returns frozensets so the cached value can't be mutated by a caller."""
    return {t: frozenset(cols) for t, cols in load_catalog().items()}


def catalog_signature(catalog: dict[str, "frozenset[str] | set[str]"]) -> str:
    """Short human-readable 'table(col, col, ...)' rendering, used in the
    Critic's feedback so a repair prompt can see exactly what columns are
    available on a table it got wrong."""
    lines = []
    for table in sorted(catalog):
        cols = ", ".join(sorted(catalog[table]))
        lines.append(f"{table}({cols})")
    return "\n".join(lines)
