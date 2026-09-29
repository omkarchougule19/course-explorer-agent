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
finally:
    agent._subject_coverage = real_cov

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

# 6. runaway table padding (a model once padded a header with 2.1M spaces)
check("padding runs collapse; the table still parses",
      agent.collapse_padding("| A    | B      |\n|------|--------|") == "| A | B |\n|---|---|")
check("ordinary text is left alone",
      agent.collapse_padding("two  spaces - and --- dashes") == "two  spaces - and --- dashes")
check("a tail of pure padding counts as a runaway", agent._is_runaway(["ok"] + [" "] * 400))
check("a short pad or real text does not", not agent._is_runaway([" "] * 50)
      and not agent._is_runaway(["text " * 100]))

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
