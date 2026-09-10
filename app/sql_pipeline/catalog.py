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


@lru_cache(maxsize=1)
def get_catalog() -> dict[str, frozenset[str]]:
    """{table_name: {column, ...}} for every INCLUDED_TABLES entry that
    actually exists in the live database (columns lowercased; via
    db.existing_columns, which abstracts PRAGMA table_info vs
    information_schema). A missing table is left out - the Critic then flags
    any query referencing it, which is the correct outcome. Process-cached:
    the schema doesn't change within a run and re-reading it per question
    adds a round-trip to Neon. Frozensets so a caller can't mutate the cache."""
    conn = db.get_connection()
    try:
        out: dict[str, frozenset[str]] = {}
        for table in INCLUDED_TABLES:
            cols = frozenset(c.lower() for c in db.existing_columns(conn, table))
            if cols:
                out[table.lower()] = cols
        return out
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


def catalog_signature(catalog: dict[str, "frozenset[str] | set[str]"]) -> str:
    """Short human-readable 'table(col, col, ...)' rendering, used in the
    Critic's feedback so a repair prompt can see exactly what columns are
    available on a table it got wrong."""
    lines = []
    for table in sorted(catalog):
        cols = ", ".join(sorted(catalog[table]))
        lines.append(f"{table}({cols})")
    return "\n".join(lines)
