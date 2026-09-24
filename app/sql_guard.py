"""
sql_guard.py

Structural checks on LLM-written SQL before it reaches the database. Shared by
both answer paths: the LangChain agent's sql_db_query tool (agent.py's
_CappedSQLDatabase) and the Generator -> Critic -> Repair pipeline
(sql_pipeline/graph.py's _run_sql).

The prompt (SYSTEM_CONTEXT) is the first line of defence against a jailbreak;
this is the second, and the read-only transaction + least-privilege role
(`readonly_engine()` / db.get_readonly_connection(), DEPLOYMENT.md §3.5) is
the third. None of them relies on another.

check_select() rejects, by parsing with sqlglot rather than by prefix
matching (a `WITH d AS (DELETE ... RETURNING *) SELECT ...` starts with
"with" and has no semicolon):
    - anything but exactly one statement
    - a root that isn't a query (SELECT / UNION / INTERSECT / EXCEPT)
    - any write/DDL/session node anywhere in the tree, including inside CTEs
      and subqueries (INSERT, UPDATE, DELETE, MERGE, SELECT INTO, FOR UPDATE,
      SET, COPY, GRANT, ...)
    - any table outside the caller's allowlist, or schema-qualified outside
      the default schema - this is what keeps the agent out of ask_log /
      answer_feedback / site_feedback (other users' IPs and questions) and
      pg_catalog / information_schema
    - a small denylist of server-side functions that sleep, read files, open
      connections, change settings or run nested SQL
"""

import os
from typing import Iterable, Optional

import sqlglot
from sqlglot import exp

# Per-statement ceiling for LLM-written SQL on Postgres. Bounds a runaway
# cross join / recursive CTE / generate_series the function denylist can't
# know about.
STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_STATEMENT_TIMEOUT_MS", "8000"))

_QUERY_ROOTS = (exp.Select, exp.SetOperation)

_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.TruncateTable, exp.Command, exp.Into, exp.Set, exp.Copy,
    exp.Grant, exp.Commit, exp.Rollback, exp.Transaction, exp.Pragma, exp.Use,
    exp.LoadData, exp.Lock,
)

_FORBIDDEN_FUNCS = frozenset({
    # sleeping / locking
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_advisory_lock",
    "pg_advisory_xact_lock", "pg_advisory_lock_shared", "pg_try_advisory_lock",
    # files, large objects, other servers
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_from_bytea", "dblink",
    "dblink_exec", "dblink_connect",
    # settings and other sessions
    "set_config", "current_setting", "pg_reload_conf", "pg_terminate_backend",
    "pg_cancel_backend",
    # nested SQL execution
    "query_to_xml", "query_to_xml_and_xmlschema", "cursor_to_xml",
    "table_to_xml", "schema_to_xml", "database_to_xml",
    # SQLite extensions / huge allocations
    "load_extension", "readfile", "writefile", "randomblob", "zeroblob",
})

_DEFAULT_SCHEMAS = ("", "public", "main")


class SQLGuardError(ValueError):
    """Raised when a statement fails check_select()."""


def _dialect(name: str) -> str:
    """Map a SQLAlchemy / app backend name onto sqlglot's dialect name."""
    name = (name or "").lower()
    return "postgres" if name.startswith("postgres") else "sqlite"


def check_select(sql: str, dialect: str, allowed_tables: Iterable[str]) -> Optional[str]:
    """None if `sql` is a single read-only query over `allowed_tables`, else a
    short reason (worded for the model to act on - it's returned to the agent
    as the tool's error)."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=_dialect(dialect)) if s is not None]
    except Exception as exc:  # sqlglot.errors.ParseError and friends
        return f"could not parse the SQL ({str(exc).splitlines()[0][:200]})"
    if len(statements) != 1:
        return "exactly one SQL statement is allowed"
    tree = statements[0]
    if not isinstance(tree, _QUERY_ROOTS):
        return "only SELECT queries are allowed"

    bad = tree.find(*_FORBIDDEN_NODES)
    if bad is not None:
        return f"only read-only SELECT queries are allowed ({type(bad).__name__.upper()} found)"

    for fn in tree.find_all(exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if name in _FORBIDDEN_FUNCS:
            return f"function {name}() is not allowed"

    allowed = {t.lower() for t in allowed_tables}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:  # a table-valued function; its name was checked above
            continue
        if (table.db or "").lower() not in _DEFAULT_SCHEMAS:
            return f"table {table.db}.{table.name} is not available"
        if name not in allowed and name not in ctes:
            return f"table {table.name} is not available; use only: {', '.join(sorted(allowed))}"
    return None


def assert_select(sql: str, dialect: str, allowed_tables: Iterable[str]) -> None:
    reason = check_select(sql, dialect, allowed_tables)
    if reason:
        raise SQLGuardError(reason)


def readonly_engine(engine):
    """Make every connection from a SQLAlchemy engine read-only, whatever role
    its URL carries. Postgres: each transaction opens with SET TRANSACTION READ
    ONLY and a SET LOCAL statement_timeout - transaction-scoped on purpose, so
    it holds through Neon's transaction-mode pooler. SQLite: PRAGMA
    query_only on every new connection. Returns the engine."""
    from sqlalchemy import event

    if engine.dialect.name == "postgresql":
        @event.listens_for(engine, "begin")
        def _begin_read_only(conn):
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS:d}")
    elif engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _query_only(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA query_only = ON")
    return engine
