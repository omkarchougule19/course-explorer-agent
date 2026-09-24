"""
test_sql_guard.py

Offline checks (no LLM, no network) for app/sql_guard.py and the read-only
execution paths that use it:

    .venv/Scripts/python -m evals.test_sql_guard

1. check_select() lets ordinary analytical SELECTs through (CTEs, joins,
   subqueries, UNION, Postgres casts/ILIKE) and rejects writes, stacked
   statements, data-modifying CTEs, SELECT INTO, FOR UPDATE, catalog/log
   tables and dangerous functions - on both dialects.
2. _CappedSQLDatabase refuses a rejected statement with a SQLAlchemyError
   (so the toolkit hands it back to the model) and never executes it.
3. readonly_engine() makes a SQLite engine refuse writes even for SQL that
   bypasses the guard.
4. The pipeline's _run_sql refuses the same statements.
5. db.readonly_database_url() refuses to fall back to the owner role on
   Render unless ALLOW_RW_AGENT_DB is set.
"""

import os
import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text

from app import agent, db, sql_guard

failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


TABLES = agent.INCLUDED_TABLES

ALLOWED = [
    "SELECT subject, COUNT(*) FROM sections WHERE year = 2026 GROUP BY subject LIMIT 5",
    "WITH c AS (SELECT subject, course_number FROM sections) SELECT * FROM c",
    "SELECT s.crn, m.start_time FROM sections s JOIN meetings m ON m.crn = s.crn "
    "AND m.year = s.year WHERE s.subject = 'CS'",
    "SELECT DISTINCT s.subject FROM sections s WHERE NOT EXISTS (SELECT 1 FROM "
    "prerequisites p WHERE p.subject = s.subject)",
    "SELECT subject FROM sections UNION SELECT subject FROM gen_ed_categories",
    "SELECT title, event_date FROM academic_calendar WHERE category IN ('drop','withdraw')",
    "SELECT subject FROM sections WHERE instructor LIKE 'Smith%';",
]
ALLOWED_PG = [
    "SELECT course_number::int FROM sections WHERE instructor ILIKE '%smith%'",
    "SELECT string_agg(DISTINCT instructor, ', ') FROM sections",
]

REJECTED = {
    "DROP TABLE sections": "DROP",
    "DELETE FROM sections": "DELETE",
    "UPDATE sections SET instructor = 'x'": "UPDATE",
    "INSERT INTO sections (crn) VALUES ('1')": "INSERT",
    "SELECT 1; DROP TABLE sections": "stacked statements",
    "WITH d AS (DELETE FROM sections RETURNING *) SELECT * FROM d": "data-modifying CTE",
    "SELECT * INTO stolen FROM sections": "SELECT INTO",
    "SELECT * FROM sections FOR UPDATE": "FOR UPDATE",
    "SELECT client_ip, question FROM ask_log": "ask_log (other users' IPs)",
    "SELECT * FROM answer_feedback": "answer_feedback",
    "SELECT * FROM site_feedback": "site_feedback",
    "SELECT * FROM sections WHERE crn IN (SELECT client_ip FROM ask_log)": "log table in subquery",
    "SELECT * FROM pg_catalog.pg_user": "pg_catalog",
    "SELECT * FROM information_schema.tables": "information_schema",
    "SELECT * FROM pg_roles": "unqualified catalog table",
    "SELECT pg_sleep(60)": "pg_sleep",
    "SELECT * FROM pg_ls_dir('.')": "pg_ls_dir",
    "SELECT set_config('statement_timeout', '0', false)": "set_config",
    "SELECT query_to_xml('select * from ask_log', true, true, '')": "nested SQL",
    "SET statement_timeout = 0": "SET",
    "": "empty",
    "this is not sql": "unparseable",
}

for sql in ALLOWED:
    for dialect in ("postgres", "sqlite"):
        check(f"allowed [{dialect}]: {sql[:60]}", sql_guard.check_select(sql, dialect, TABLES) is None)
for sql in ALLOWED_PG:
    check(f"allowed [postgres]: {sql[:60]}", sql_guard.check_select(sql, "postgresql", TABLES) is None)
for sql, what in REJECTED.items():
    check(f"rejected: {what}", sql_guard.check_select(sql, "postgres", TABLES) is not None)
check("rejected on sqlite too: data-modifying CTE",
      sql_guard.check_select("WITH d AS (DELETE FROM sections RETURNING *) SELECT * FROM d",
                             "sqlite", TABLES) is not None)

# 2 + 3: the agent's SQL tool, on a throwaway SQLite file
tmp = Path(tempfile.mkdtemp()) / "t.db"
con = sqlite3.connect(tmp)
con.execute("CREATE TABLE sections (crn TEXT, subject TEXT)")
con.execute("CREATE TABLE ask_log (client_ip TEXT, question TEXT)")
con.execute("INSERT INTO sections VALUES ('1', 'CS')")
con.execute("INSERT INTO ask_log VALUES ('1.2.3.4', 'secret')")
con.commit()
con.close()

engine = sql_guard.readonly_engine(create_engine(f"sqlite:///{tmp.as_posix()}"))
sdb = agent._CappedSQLDatabase(engine, include_tables=["sections"])
check("agent tool runs a normal SELECT", "CS" in sdb.run("SELECT subject FROM sections"))
out = sdb.run_no_throw("DELETE FROM sections")
check("agent tool returns a rejection to the model", out.startswith("Error:") and "rejected" in out)
out = sdb.run_no_throw("SELECT client_ip FROM ask_log")
check("agent tool cannot read ask_log", "1.2.3.4" not in out and "rejected" in out)
con = sqlite3.connect(tmp)
check("rejected DELETE never executed", con.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 1)
con.close()
try:
    with engine.begin() as c:
        c.execute(text("DELETE FROM sections"))
    blocked = False
except Exception:
    blocked = True
check("read-only engine blocks a write that skips the guard", blocked)

# 4: the pipeline's executor, pointed at the same file
from app.sql_pipeline import graph  # noqa: E402

saved_url = os.environ.pop("DATABASE_URL", None)
saved_path = db.DB_PATH
db.DB_PATH = tmp
try:
    rows, err = graph._run_sql("SELECT subject FROM sections")
    check("pipeline runs a normal SELECT", err is None and rows == [{"subject": "CS"}])
    rows, err = graph._run_sql("WITH d AS (DELETE FROM sections RETURNING *) SELECT * FROM d")
    check("pipeline rejects a data-modifying CTE", rows is None and err)
    rows, err = graph._run_sql("SELECT client_ip FROM ask_log")
    check("pipeline cannot read ask_log", rows is None and err)
    raw = db.get_readonly_connection()
    try:
        raw.execute("DELETE FROM sections")
        ro = False
    except sqlite3.OperationalError:
        ro = True
    finally:
        raw.close()
    check("pipeline connection is read-only", ro)
finally:
    db.DB_PATH = saved_path
    if saved_url is not None:
        os.environ["DATABASE_URL"] = saved_url

# 5: fail closed on Render without a read-only role
keys = ("DATABASE_URL", "DATABASE_URL_RO", "RENDER", "ALLOW_RW_AGENT_DB")
saved = {k: os.environ.get(k) for k in keys}


def ro_url(**env):
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        return db.readonly_database_url()
    except RuntimeError:
        return "RAISED"


check("RO URL preferred", ro_url(DATABASE_URL="rw", DATABASE_URL_RO="ro", RENDER="true") == "ro")
check("Render without RO URL fails closed", ro_url(DATABASE_URL="rw", RENDER="true") == "RAISED")
check("explicit opt-out allows the owner role",
      ro_url(DATABASE_URL="rw", RENDER="true", ALLOW_RW_AGENT_DB="1") == "rw")
check("local dev falls back to DATABASE_URL", ro_url(DATABASE_URL="rw") == "rw")
check("no URL -> SQLite", ro_url() is None)
check("RO URL alone doesn't switch SQLite mode to Postgres", ro_url(DATABASE_URL_RO="ro") is None)
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
