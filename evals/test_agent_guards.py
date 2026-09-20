"""
test_agent_guards.py

Offline checks (no LLM, no network) for the guards around the SQL agent:

    .venv/Scripts/python -m evals.test_agent_guards

1. friendly_stop() swaps LangChain's raw "Agent stopped due to max iterations."
   for a message a student can act on, and leaves real answers alone.
2. That friendly message is tagged `error` by ask_log.classify_answer, so it
   does not count against a user's rate limit.
3. _build_llm() picks Groq first, then OpenAI, then Gemini, by which key is set
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

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
