"""
test_agent_guards.py

Offline checks (no LLM, no network) for the guards around the SQL agent:

    .venv/Scripts/python -m evals.test_agent_guards

1. friendly_stop() swaps LangChain's raw "Agent stopped due to max iterations."
   for a message a student can act on, and leaves real answers alone.
2. That friendly message is tagged `error` by ask_log.classify_answer, so it
   does not count against a user's rate limit.
3. _build_llm() picks Groq first, then OpenAI, by which key is set; a Gemini
   key alone is ignored (Gemini support was removed 2026-09-28)
   (no network: building a client does not call the provider).
4. _CappedSQLDatabase cuts a huge query result down to a bounded size, keeps
   the row structure valid, and tells the model the result was truncated.
"""

import sqlite3
import tempfile
from pathlib import Path

from app import agent
from app.ask_log import classify_answer

failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


# 1. iteration-cap message
check("raw cap string is replaced", agent.friendly_stop(agent.AGENT_STOPPED_RAW) == agent.AGENT_STOPPED_FRIENDLY)
check("cap string with a citation footer is replaced too",
      agent.friendly_stop(agent.AGENT_STOPPED_RAW + "\n\n---\n*Sources: x*") == agent.AGENT_STOPPED_FRIENDLY)
check("a normal answer is untouched", agent.friendly_stop("CS 225 has 12 sections.") == "CS 225 has 12 sections.")
check("empty answer is untouched", agent.friendly_stop("") == "")

# 2. not counted against the rate limit
check("friendly cap message is classified as an error", classify_answer(agent.AGENT_STOPPED_FRIENDLY) == "error")

# 3. provider order
import os
saved = {k: os.environ.get(k) for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER")}


def provider_with(**keys):
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in keys.items() if v})
    try:
        return agent._build_llm()[1]
    except EnvironmentError:
        return "none"


check("all keys set -> Groq first", provider_with(GROQ_API_KEY="x", OPENAI_API_KEY="x", GEMINI_API_KEY="x") == "Groq")
check("no Groq key -> OpenAI second", provider_with(OPENAI_API_KEY="x", GEMINI_API_KEY="x") == "OpenAI")
check("no keys -> clear error", provider_with() == "none")
check("a Gemini key alone is ignored", provider_with(GEMINI_API_KEY="x") == "none")
check("LLM_PROVIDER=openai overrides the order",
      provider_with(GROQ_API_KEY="x", OPENAI_API_KEY="x", LLM_PROVIDER="openai") == "OpenAI")
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v

# 4. result cap
tmp = Path(tempfile.mkdtemp()) / "t.db"
con = sqlite3.connect(tmp)
con.execute("CREATE TABLE t (a TEXT, b TEXT)")
con.executemany("INSERT INTO t VALUES (?, ?)", [(f"row{i:05d}", "x" * 40) for i in range(2000)])
con.commit()
con.close()
db = agent._CappedSQLDatabase.from_uri(f"sqlite:///{tmp.as_posix()}")
big = db.run("SELECT a, b FROM t")
check("large result is truncated", len(big) < agent.MAX_QUERY_RESULT_CHARS + 400)
check("truncation note is present", "[Result truncated" in big)
check("kept rows still end on a whole tuple", big.split("\n")[0].endswith(")]"))
check("the default layout is LangChain's list of tuples",
      db.run("SELECT a, b FROM t LIMIT 1") == "[('row00000', '" + "x" * 40 + "')]")
# the tab layout (SQL_RESULT_LAYOUT=tabs): fewer tokens, off by default
os.environ["SQL_RESULT_LAYOUT"] = "tabs"
big = db.run("SELECT a, b FROM t ORDER BY a")
big_rows = big.split("\n[Result truncated")[0].split("\n")
check("tabs: kept rows are whole tab-separated rows, with no header line",
      big_rows[0].startswith("row00000\t") and all(r.startswith("row") and r.endswith("\t" + "x" * 40) for r in big_rows))
check("tabs: the note counts rows, not characters",
      f"2,000 rows returned, only the first {len(big_rows):,} shown" in big)
con = sqlite3.connect(tmp)
con.execute("CREATE TABLE n (a TEXT, b TEXT, c INT)")
con.execute("INSERT INTO n VALUES ('x\ty', NULL, 3), ('two\nlines', 'b', NULL)")
con.commit()
con.close()
db = agent._CappedSQLDatabase.from_uri(f"sqlite:///{tmp.as_posix()}")
check("tabs: a missing value is NULL; a value can't break the layout",
      db.run("SELECT a, b, c FROM n") == "x y\tNULL\t3\ntwo lines\tb\tNULL")
check("tabs: no rows is still an empty result", db.run("SELECT a FROM n WHERE c = 99") == "")
check("tabs: a single value is just the value", db.run("SELECT COUNT(*) AS total FROM n") == "2")
os.environ.pop("SQL_RESULT_LAYOUT")
check("no rows is an empty result in the default layout too", db.run("SELECT a FROM n WHERE c = 98") == "")
small = db.run("SELECT a, b FROM t LIMIT 3")
check("small result is untouched", "[Result truncated" not in small and small.count("row0") == 3)

# 4b. an identical query repeated within one answer is not re-run
check("no log outside an answer: plain result", "[You already ran" not in db.run("SELECT a FROM t LIMIT 1"))
agent.new_query_log()
first = db.run("SELECT a FROM t LIMIT 2")
again = db.run("SELECT  a FROM t\n LIMIT 2")   # whitespace differs, same query
check("a repeated query returns the earlier result with a note",
      again.startswith("[You already ran this exact query") and again.endswith(first))
bad = "SELECT a FROM t WHERE (a = 'x'"
try:
    db.run(bad)
except Exception:
    pass
try:
    db.run(bad)
    repeated_error = ""
except Exception as exc:
    repeated_error = str(exc)
check("a repeated failing query is refused with its first error", "Repeated query" in repeated_error)
agent.new_query_log()
check("a new answer starts a fresh log", "[You already ran" not in db.run("SELECT a FROM t LIMIT 2"))

# 4e. the data budget: all results of one answer share what the request has
# room for, so a second broad query can't push the request past the
# provider's size limit (Groq 413 on "tell me about badm sections")
# exact counts (o200k, which Groq's count matched): 12 and 34 tokens
prose_text = "Business Analytics I covers data visualization and decision making for managers."
rows_text = "crn\tcourse_number\tinstructor\n10398\t199\tGinsburg, R\n29648\t210\t\n53818\t554\tLuckman, E"
check("the token estimate is close, and never low",
      12 <= agent._est_tokens(prose_text) <= 18 and 34 <= agent._est_tokens(rows_text) <= 41)
check("an empty text costs nothing", agent._est_tokens("") == 0 and agent._est_tokens(None) == 0)
agent._DATA_BUDGET.set({"left": 600})
one = db.run("SELECT a, b FROM t ORDER BY a")
check("a result is cut to the room left, on a whole row",
      agent._est_tokens(one) < 600 + 120 and one.split("\n[Result truncated")[0].endswith(")]")
      and "little room left for data" in one)
check("the result is charged to the budget", agent._DATA_BUDGET.get()["left"] < 100)
two = db.run("SELECT a, b FROM t ORDER BY a DESC")
check("a later result still gets a few rows, never nothing",
      "row01999" in two and agent._est_tokens(two) < agent._MIN_RESULT_TOKENS + 120)
agent._DATA_BUDGET.set({"left": 300})
fitted = agent.fit_budget("\n\n".join("paragraph %d " % i + "word " * 40 for i in range(20)))
check("a non-SQL tool result is cut at a paragraph and says so",
      agent._est_tokens(fitted) < 300 + 80 and "[Cut to fit" in fitted and "paragraph 0" in fitted)
check("a short result passes through", agent.fit_budget("short") == "short")
agent._DATA_BUDGET.set(None)
check("without a budget nothing is cut", len(agent.fit_budget("y" * 9000)) == 9000)
saved_limit = os.environ.get("LLM_REQUEST_TOKEN_LIMIT")
os.environ["LLM_REQUEST_TOKEN_LIMIT"] = "0"
agent.new_query_log("q")
check("LLM_REQUEST_TOKEN_LIMIT=0 turns the budget off", agent._DATA_BUDGET.get() is None)
os.environ["LLM_REQUEST_TOKEN_LIMIT"] = "8000"
agent.new_query_log("q", "q")
short_room = agent._DATA_BUDGET.get()["left"]
check("the budget leaves room for the reply",
      short_room <= 8000 - agent._REPLY_RESERVE_TOKENS - agent._est_tokens(agent.SYSTEM_CONTEXT))
agent.new_query_log("q", "q " + "word " * 300)
check("history in the request leaves less room for data",
      agent._DATA_BUDGET.get()["left"] == max(short_room - 300, agent._MIN_ANSWER_DATA_TOKENS))
os.environ["LLM_REQUEST_TOKEN_LIMIT"] = "3000"
agent.new_query_log("q", "q")
check("the budget never drops below the floor", agent._DATA_BUDGET.get()["left"] == agent._MIN_ANSWER_DATA_TOKENS)
if saved_limit is None:
    os.environ.pop("LLM_REQUEST_TOKEN_LIMIT", None)
else:
    os.environ["LLM_REQUEST_TOKEN_LIMIT"] = saved_limit
agent._DATA_BUDGET.set(None)
too_large = agent.friendly_error(Exception(
    "Error code: 413 - {'error': {'message': 'Request too large for model `x` on tokens per minute "
    "(TPM): Limit 8000, Requested 8728', 'code': 'rate_limit_exceeded'}}"))
check("a request-too-large error tells the student to narrow the question", too_large == agent.REQUEST_TOO_LARGE)
check("... and is tagged as an error, not counted against the rate limit", classify_answer(too_large) == "error")

# 4f. the empty-answer fallback is a failure, not an answer
check("the empty-answer fallback is tagged as an error (no rate-limit charge, no Sources footer)",
      classify_answer(agent.NO_ANSWER) == "error")

# 4g. the date in the prompt follows the calendar, not the process's start
real_today = agent._today
try:
    agent._today = lambda: "2026-10-09"
    monday = agent._data_notes()
    agent._today = lambda: "2026-10-16"
    week_later = agent._data_notes()
    check("DATA NOTES are rebuilt when the day changes",
          (not monday and not week_later)   # no catalog in this test database: notes are empty
          or ("Today is 2026-10-09" in monday and "Today is 2026-10-16" in week_later))
    built = []
    real_new_agent, agent._new_agent = agent._new_agent, lambda **kw: built.append(kw) or object()
    try:
        agent._AGENTS.clear()
        first = agent.build_agent()
        check("the agent is reused within a day", agent.build_agent() is first and len(built) == 1)
        agent._today = lambda: "2026-10-17"
        check("... and rebuilt on a new day (its prompt holds the date)",
              agent.build_agent() is not first and len(built) == 2 and len(agent._AGENTS) == 1)
    finally:
        agent._new_agent = real_new_agent
        agent._AGENTS.clear()
finally:
    agent._today = real_today

# 4c. empty result for a subject with no rows in the latest term
real_cov = agent._subject_coverage
agent._subject_coverage = lambda: ("fall 2026", {"CS": "fall 2026", "ECON": "spring 2026"})
try:
    note = agent._unsynced_note("SELECT * FROM sections WHERE subject = 'ECON' AND semester = 'fall' AND year = 2026")
    check("unsynced subject in the latest term gets a note naming its latest term",
          bool(note) and "ECON has no fall 2026 rows" in note and "spring 2026" in note)
    check("a synced subject gets no note",
          agent._unsynced_note("SELECT * FROM sections WHERE subject = 'CS' AND semester = 'fall' AND year = 2026") is None)
    check("a query on another term gets no note",
          agent._unsynced_note("SELECT * FROM sections WHERE subject = 'ECON' AND semester = 'spring' AND year = 2026") is None)
    # every code of an IN list is checked, not only the first (CS is synced, ECON isn't)
    note = agent._unsynced_note("SELECT * FROM sections WHERE subject IN ('CS', 'ECON') AND semester = 'fall' AND year = 2026")
    check("an unsynced subject later in an IN list still gets the note",
          bool(note) and "ECON has no fall 2026 rows" in note and "CS has" not in note)
finally:
    agent._subject_coverage = real_cov

from app import sql_guard  # noqa: E402
check("subject codes: =, a table alias, UPPER() and every code of an IN list",
      sql_guard.subject_codes("SELECT 1 FROM sections s WHERE s.subject = 'cs' OR UPPER(subject) = 'ECE' "
                              "OR subject IN ('ACCY', 'badm', 'CS')") == ["CS", "ECE", "ACCY", "BADM"])
check("subject codes: req_subject and other columns are not subjects",
      sql_guard.subject_codes("SELECT 1 FROM prerequisites WHERE req_subject = 'MATH' AND subject='STAT'") == ["STAT"])

# 4h. caches about the catalog are dropped when the catalog changes, not at restart
real_stamp, real_check = agent._catalog_stamp, agent._CATALOG_CHECK_SECONDS
stamps = iter([("2026-09-28", 100), ("2026-09-28", 100), ("2026-10-09", 140)])
calls = []
try:
    agent._catalog_stamp = lambda: calls.append(1) or next(stamps)
    agent._CATALOG.update(checked=0.0, stamp=None)
    agent._CATALOG_CHECK_SECONDS = 0
    agent._AGENTS[("x",)] = object()
    agent._subject_names.cache_clear()
    agent._subject_names()
    check("first look at the catalog only records it", agent.refresh_catalog_caches() is False and len(agent._AGENTS) == 1)
    check("an unchanged catalog keeps the caches",
          agent.refresh_catalog_caches() is False and agent._subject_names.cache_info().currsize == 1)
    check("a synced department drops them (coverage, names, DATA NOTES, the built agents)",
          agent.refresh_catalog_caches() is True and agent._subject_names.cache_info().currsize == 0
          and not agent._AGENTS)
    agent._CATALOG_CHECK_SECONDS = 600
    check("the check is skipped inside its interval", agent.refresh_catalog_caches() is False and len(calls) == 3)

    def broken():
        raise RuntimeError("database down")
    agent._catalog_stamp = broken
    check("a database error leaves the caches alone", agent.refresh_catalog_caches(force=True) is False)
finally:
    agent._catalog_stamp, agent._CATALOG_CHECK_SECONDS = real_stamp, real_check
    agent._CATALOG.update(checked=0.0, stamp=None)
    agent._AGENTS.clear()

# 4d. empty result for a full-first-name instructor
n = agent._instructor_note("SELECT * FROM sections WHERE instructor = 'Ji, Heng'")
check("a full first name gets the last-name + initial hint",
      bool(n) and "instructor_last = 'ji' AND instructor_initial = 'h'" in n)
check("any empty instructor lookup gets the format hint",
      "no first names" in (agent._instructor_note("SELECT * FROM sections WHERE instructor LIKE '%Vishal%'") or ""))
check("queries without an instructor get no hint", agent._instructor_note("SELECT * FROM sections WHERE subject = 'CS'") is None)
check("rows for 'Last, F' carry the initial-match note",
      "match by initial" in (agent._initial_note("SELECT * FROM sections WHERE instructor = 'Ji, H'") or ""))
check("no initial note for other queries", agent._initial_note("SELECT * FROM sections WHERE subject = 'CS'") is None)

# 5. the agent is built once per process and never blocks the event loop
import asyncio
import time

from app import db as appdb

env_keys = ("DATABASE_URL", "DATABASE_URL_RO", "GROQ_API_KEY", "GROQ_MODEL", "LLM_PROVIDER")
saved5 = {k: os.environ.get(k) for k in env_keys}
saved_paths = (agent.DB_PATH, appdb.DB_PATH)
cat = Path(tempfile.mkdtemp()) / "catalog.db"
con = sqlite3.connect(cat)
con.execute("CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, crn TEXT)")
con.execute("INSERT INTO sections VALUES (2026, 'fall', 'CS', '225', '1')")
for t in agent.INCLUDED_TABLES:  # SQLDatabase requires every included table to exist
    if t != "sections":
        con.execute(f"CREATE TABLE {t} (id INTEGER)")
con.commit()
con.close()
for k in env_keys:
    os.environ.pop(k, None)
os.environ.update(DATABASE_URL="", DATABASE_URL_RO="", GROQ_API_KEY="k")  # fake key: no network on build
agent.DB_PATH = appdb.DB_PATH = cat
try:
    a1, a2 = agent.build_agent(), agent.build_agent()
    check("second build_agent() returns the cached agent", a1 is a2)
    check("streaming and non-streaming agents are cached separately",
          agent.build_agent(streaming=True) is not a1)
    os.environ["GROQ_MODEL"] = "openai/gpt-oss-20b"
    check("a different model setting builds a new agent", agent.build_agent() is not a1)
    check("LLM clients are reused", agent._build_llm()[0] is agent._build_llm()[0])

    agent._SECTIONS_SEEN = False
    first = agent._sections_empty()
    real_conn = appdb.get_connection
    appdb.get_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError("queried again"))
    try:
        second = agent._sections_empty()
    finally:
        appdb.get_connection = real_conn
    check("non-empty sections is remembered (no second query)", first is False and second is False)

    real_build = agent.build_agent

    def slow_build(**kw):
        time.sleep(0.5)          # a first build, standing in for ~1.5 s of reflection
        return object()

    async def max_gap():
        gaps, stop = [], asyncio.Event()

        async def tick():
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        t = asyncio.create_task(tick())
        await agent._prepare_agent_async(streaming=True)
        stop.set()
        await t
        return max(gaps)

    agent.build_agent = slow_build
    try:
        gap = asyncio.run(max_gap())
    finally:
        agent.build_agent = real_build
    check(f"a slow build doesn't block the event loop (longest gap {gap * 1000:.0f} ms)", gap < 0.2)
finally:
    agent.DB_PATH, appdb.DB_PATH = saved_paths
    for k, v in saved5.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v

# 5b. a client's retry wait shows up as a status while the stream is blocked
import logging as _logging


async def _merged():
    q = asyncio.Queue()
    agent._NOTICE_TARGET.set((asyncio.get_running_loop(), q))

    async def source():
        yield {"event": "first"}
        _logging.getLogger("groq._base_client").info(
            "Retrying request to /openai/v1/chat/completions in 2.400000 seconds")
        await asyncio.sleep(0.3)          # the client sleeping before its retry
        yield {"event": "second"}

    return [x async for x in agent._with_notices(source(), q)]


out = asyncio.run(_merged())
check("a retry wait becomes a notice before the next event",
      [k for k, _ in out] == ["event", "notice", "event"] and "waiting about 2 s" in out[1][1])
agent._NOTICE_TARGET.set(None)
_logging.getLogger("groq._base_client").info("Retrying request to /x in 5 seconds")   # no target: ignored
check("no stream waiting -> the log line is ignored", True)

# 6. runaway table padding (a model once padded a header with 2.1M spaces)
check("padding runs collapse; the table still parses",
      agent.collapse_padding("| A    | B      |\n|------|--------|") == "| A | B |\n|---|---|")
check("tidy_answer decodes entities the model wrote",
      agent.tidy_answer("Programming Languages &amp; Compilers  ") == "Programming Languages & Compilers")
check("tidy_answer leaves markup as text for the renderer to escape",
      agent.tidy_answer("&lt;b&gt;x&lt;/b&gt;") == "<b>x</b>")
check("internal comparisons become plain words",
      agent.tidy_answer("courses where `grad_credit = 'yes'` in the subjects table") ==
      "courses where open to graduate students in the department list")
check("bare column names become plain words", "course titles" in agent.tidy_answer("matched on title_search"))
check("mangled instructor links are repaired",
      agent.tidy_answer("[Sun, E](?/instructor.html?name=Sun%2C%20E) and [Hall, G](https://instructor.html?name=Hall)")
      == "[Sun, E](/instructor.html?name=Sun%2C%20E) and [Hall, G](/instructor.html?name=Hall)")
check("good links are untouched", agent.tidy_answer("[CS 225](/?course=CS-225)") == "[CS 225](/?course=CS-225)")
check("ordinary text is left alone",
      agent.collapse_padding("two  spaces - and --- dashes") == "two  spaces - and --- dashes")
check("a tail of pure padding counts as a runaway", agent._is_runaway(["ok"] + [" "] * 400))
check("a short pad or real text does not", not agent._is_runaway([" "] * 50)
      and not agent._is_runaway(["text " * 100]))

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
