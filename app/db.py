"""
db.py

Unified database client for this app's dual-backend design: local SQLite for
development, Neon Postgres in production. Uses Postgres when DATABASE_URL is
set in the environment, otherwise falls back to the local SQLite file at
data/courses.db - so nobody needs a Postgres instance running just to develop
or run tests locally.

Every query string elsewhere in this codebase (scraper.py, api.py, the
load_*.py scripts) is written once, using '?' placeholders and SQLite-ish
syntax (INSERT OR REPLACE, PRAGMA table_info). This module is what makes that
one query text work against both backends:

  - Connection.execute()/.executemany() transparently translate '?' -> '%s'
    when the backend is Postgres (a no-op on SQLite, which accepts '?'
    natively) - so callers never write backend-specific placeholders.
  - upsert() replaces "INSERT OR REPLACE" call sites with the right statement
    per backend (SQLite's own OR REPLACE vs. Postgres's
    INSERT ... ON CONFLICT ... DO UPDATE).
  - existing_columns() replaces "PRAGMA table_info" for the
    add-a-column-if-missing migration pattern used when a table gains a field.
  - autoincrement_pk() / current_timestamp_default() cover the couple of
    DDL keywords (AUTOINCREMENT vs SERIAL) that differ between the two.

Rows come back dict-like on both backends (sqlite3.Row / psycopg2's
RealDictRow), so existing code like `dict(row)` or `row["column"]` keeps
working unchanged regardless of which database is active.

psycopg2 is only imported when DATABASE_URL is actually set, so a purely
local/SQLite setup never needs it installed.
"""

import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence

DB_PATH = Path(__file__).parent.parent / "data" / "courses.db"

_PLACEHOLDER_RE = re.compile(r"\?")


def is_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _translate(query: str) -> str:
    """'?' -> '%s' for psycopg2. None of this codebase's queries use a
    literal '?' inside a string constant, so a blanket replace is safe."""
    return _PLACEHOLDER_RE.sub("%s", query)


class Connection:
    """Wraps a raw sqlite3 or psycopg2 connection behind one interface, so
    calling code writes a single code path regardless of backend."""

    def __init__(self, raw, backend: str):
        self._raw = raw
        self.backend = backend  # "sqlite" or "postgres"

    def execute(self, query: str, params: Sequence = ()):
        if self.backend == "postgres":
            import psycopg2.extras
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(_translate(query), params)
        else:
            cur = self._raw.execute(query, params)
        return cur

    def executemany(self, query: str, seq_of_params: Iterable[Sequence]):
        seq_of_params = list(seq_of_params)
        if not seq_of_params:
            return None
        if self.backend == "postgres":
            cur = self._raw.cursor()
            cur.executemany(_translate(query), seq_of_params)
        else:
            cur = self._raw.executemany(query, seq_of_params)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def get_connection(db_path: Optional[Path] = None) -> Connection:
    """The one place either backend gets connected to. `db_path` is only used
    for the SQLite fallback; ignored when DATABASE_URL is set."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg2
        raw = psycopg2.connect(database_url)
        return Connection(raw, "postgres")

    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    return Connection(raw, "sqlite")


def existing_columns(conn: Connection, table: str) -> set:
    """Column names currently on `table`. Used for the "ALTER TABLE ADD
    COLUMN if missing" migration pattern - replaces SQLite's PRAGMA
    table_info with something that works on Postgres too."""
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        ).fetchall()
        return {r["column_name"] for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def autoincrement_pk() -> str:
    """Column definition fragment for an auto-incrementing integer primary
    key, e.g. `f"CREATE TABLE t (id {autoincrement_pk()}, ...)"`."""
    return "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"


def current_timestamp_default() -> str:
    """Column type + default for a "when was this row written" timestamp
    column. Postgres: a real TIMESTAMP. SQLite: TEXT, since that's what
    CURRENT_TIMESTAMP produces there and existing code compares it as a
    sortable ISO-ish string (see recently_scraped_courses() in scraper.py)."""
    return "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" if is_postgres() else "TEXT DEFAULT CURRENT_TIMESTAMP"


def _upsert_query(backend: str, table: str, columns: Sequence[str],
                   conflict_columns: Sequence[str]) -> str:
    """Pure query-text builder, split out from upsert() so the SQL it
    generates can be unit-tested without a live connection to either
    backend."""
    if backend == "postgres":
        update_cols = [c for c in columns if c not in conflict_columns]
        placeholders = ", ".join(["%s"] * len(columns))
        if update_cols:
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            conflict_action = f"DO UPDATE SET {set_clause}"
        else:
            conflict_action = "DO NOTHING"
        return (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(conflict_columns)}) {conflict_action}"
        )
    placeholders = ", ".join(["?"] * len(columns))
    return f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"


def upsert(conn: Connection, table: str, columns: Sequence[str], rows: Sequence[Sequence],
           conflict_columns: Sequence[str]) -> int:
    """INSERT ... ON CONFLICT DO UPDATE (Postgres) / INSERT OR REPLACE (SQLite)
    for the same (columns, rows). One helper instead of duplicating
    conflict-resolution logic per backend at every INSERT OR REPLACE call
    site in scraper.py and the load_*.py scripts.

    `conflict_columns` must match the table's UNIQUE/PRIMARY KEY constraint
    that the upsert is resolving against."""
    rows = list(rows)
    if not rows:
        return 0

    query = _upsert_query(conn.backend, table, columns, conflict_columns)
    if conn.backend == "postgres":
        cur = conn._raw.cursor()
        cur.executemany(query, rows)
    else:
        conn._raw.executemany(query, rows)
    return len(rows)
