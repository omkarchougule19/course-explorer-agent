"""
test_request_guards.py

Offline checks (no LLM, no network) for the request-level guards in
app/api.py, app/ask_log.py and app/feedback.py, run against a throwaway
SQLite database with the agent stubbed out:

    .venv/Scripts/python -m evals.test_request_guards

1. The shared daily cap holds under a burst of parallel /ask requests
   (each reserves a `pending` ask_log row before the LLM runs), and no row
   is left `pending` afterwards.
2. The concurrency ceiling turns extra simultaneous asks into fast 503s and
   gives every permit back, for /ask and /ask/stream alike.
3. Error responses don't echo driver/provider exception text.
4. Public read routes reject out-of-range input with 422 (CRN list size,
   semester, year, string lengths).
5. /ask/feedback caps new rows per IP, but re-voting an answer still works.
"""

import os

# Point the app at a throwaway SQLite file before anything imports it (the
# app's load_dotenv never overrides a variable that's already set).
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_RO"] = ""

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, api, db  # noqa: E402
from app import ask_log as ask_log_mod  # noqa: E402
from app import feedback as feedback_mod  # noqa: E402
from app.sql_pipeline import graph  # noqa: E402

failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp()) / "t.db"
con = sqlite3.connect(tmp)
con.execute("CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, "
            "course_label TEXT, crn TEXT, section_name TEXT, instructor TEXT, "
            "enrollment_status TEXT, credit_hours TEXT, description TEXT, scraped_at TEXT)")
con.execute("CREATE TABLE meetings (year INT, semester TEXT, subject TEXT, course_number TEXT, "
            "crn TEXT, meeting_type TEXT, days_of_week TEXT, start_time TEXT, end_time TEXT, "
            "building TEXT, room TEXT, instructor TEXT)")
con.execute("INSERT INTO sections VALUES (2026,'fall','CS','225','Data Structures','1','AL1',"
            "'Beckman, M','A','4',NULL,NULL)")
con.commit()
con.close()
db.DB_PATH = tmp
api.DB_PATH = tmp


def slow_ask(question, history=None):
    time.sleep(0.4)
    return "CS 225 has 12 sections."


agent.ask = slow_ask

client = TestClient(api.app)
client.__enter__()  # runs the startup hooks that create ask_log & co.


def post_ask(i):
    return client.post("/ask", json={"question": f"q{i}"},
                       headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code


def outcomes():
    c = sqlite3.connect(tmp)
    try:
        return dict(c.execute("SELECT outcome, COUNT(*) FROM ask_log GROUP BY outcome").fetchall())
    finally:
        c.close()


def clear_log():
    c = sqlite3.connect(tmp)
    c.execute("DELETE FROM ask_log")
    c.commit()
    c.close()


# 1. shared cap under a parallel burst
ask_log_mod.GLOBAL_PER_DAY = 3
api._ask_slots = threading.BoundedSemaphore(50)
with ThreadPoolExecutor(10) as pool:
    codes = list(pool.map(post_ask, range(10)))
check(f"exactly 3 of 10 parallel asks served under a cap of 3 (got {codes.count(200)})",
      codes.count(200) == 3)
check("the rest were refused with 429", codes.count(429) == 7)
o = outcomes()
check(f"log shows 3 answered, 7 global_limited, none pending ({o})",
      o.get("answered") == 3 and o.get("global_limited") == 7 and "pending" not in o)
clear_log()

# 2. concurrency ceiling, /ask
ask_log_mod.GLOBAL_PER_DAY = 1000
api._ask_slots = threading.BoundedSemaphore(2)
with ThreadPoolExecutor(6) as pool:
    codes = list(pool.map(post_ask, range(6)))
check(f"only 2 of 6 simultaneous asks run with a ceiling of 2 (got {codes})",
      codes.count(200) == 2 and codes.count(503) == 4)
check("503s wrote no ask_log rows", sum(outcomes().values()) == 2)
got = [api._ask_slots.acquire(blocking=False) for _ in range(2)]
check("every /ask permit was released", all(got))
for g in got:
    if g:
        api._ask_slots.release()
clear_log()


# concurrency ceiling, /ask/stream
async def fake_stream(question, history=None, _model=None):
    yield "status", "Running SQL…"
    yield "token", "MWF"
    yield "done", "MWF"


agent.astream_answer = fake_stream
r = client.post("/ask/stream", json={"question": "days?"})
check("stream returns the answer", r.status_code == 200 and "event: done" in r.text)
check("stream row finished as answered", outcomes() == {"answered": 1})
got = [api._ask_slots.acquire(blocking=False) for _ in range(2)]
check("every /ask/stream permit was released", all(got))
for g in got:
    if g:
        api._ask_slots.release()
blocked_sem = threading.BoundedSemaphore(1)
blocked_sem.acquire()
api._ask_slots = blocked_sem
r = client.post("/ask/stream", json={"question": "days?"})
check("stream gets a fast 503 when all slots are busy", r.status_code == 503)
api._ask_slots = threading.BoundedSemaphore(4)
clear_log()

# 3. no exception text in responses
real_get_connection = db.get_connection
db.get_connection = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("connection to host secret-host.neon.tech user neondb_owner failed"))
r = client.get("/stats")
db.get_connection = real_get_connection
check("DB-open failure is a 503 without driver text",
      r.status_code == 503 and "secret-host" not in r.text and "neondb_owner" not in r.text)
msg = agent.friendly_error(RuntimeError("psycopg2 error at secret-host.neon.tech"))
check("unrecognised agent errors are generic", "secret-host" not in msg)
check("...and still tagged as an error", ask_log_mod.classify_answer(msg) == "error")
msg = agent.setup_unavailable(RuntimeError("Couldn't open the database: secret-host"))
check("setup failures are generic", "secret-host" not in msg)
check("...and still tagged as an error", ask_log_mod.classify_answer(msg) == "error")
check("pipeline failure text doesn't quote the DB error",
      "{why}" not in open(graph.__file__, encoding="utf-8").read())

# 4. input bounds on read routes
crns51 = ",".join(str(i) for i in range(51))
bounds = {
    f"/meetings?crns={crns51}&year=2026&semester=fall": 422,
    f"/meetings?crns={'1,' * 400}&year=2026&semester=fall": 422,
    "/meetings?crns=1,2&year=2026&semester=bogus": 422,
    "/meetings?crns=1,2&year=99999999999&semester=fall": 422,
    "/meetings?crns=1,2&year=2026&semester=fall": 200,
    "/sections?semester=Fall&year=2026": 200,
    "/sections?semester=autumn": 422,
    "/sections?year=999999999999999999999": 422,
    f"/sections?instructor={'x' * 101}": 422,
    f"/sections?subject={'X' * 13}": 422,
    "/subjects?semester=nope": 422,
    "/calendar?semester=nope": 422,
    f"/courses/{'X' * 13}": 422,
    f"/courses/CS/{'9' * 11}/grade-trend": 422,
}
for url, want in bounds.items():
    got = client.get(url).status_code
    check(f"{url[:60]} -> {want} (got {got})", got == want)
r = client.post("/schedule/conflicts", json={"crns": ["1", "2"], "year": 2026, "semester": "x"})
check("conflict check rejects an unknown semester", r.status_code == 422)
r = client.post("/schedule/conflicts", json={"crns": ["1", "1"], "year": 2026, "semester": "fall"})
check("conflict check de-dupes CRNs (2 copies of one CRN is < 2 CRNs)", r.status_code == 400)

# 5. /ask/feedback caps
feedback_mod._PER_IP_DAY = 3
votes = [client.post("/ask/feedback", json={"vote": "up", "question": "q", "answer": f"a{i}"},
                     headers={"x-forwarded-for": "9.9.9.9"}).json()["ok"] for i in range(4)]
check(f"4th new vote from one IP is refused at a cap of 3 ({votes})", votes == [True, True, True, False])
r = client.post("/ask/feedback", json={"vote": "down", "question": "q", "answer": "a0"},
                headers={"x-forwarded-for": "9.9.9.9"}).json()
check("re-voting an existing answer still works at the cap", r["ok"] is True)
feedback_mod._GLOBAL_DAY = 3
r = client.post("/ask/feedback", json={"vote": "up", "question": "q", "answer": "fresh"},
                headers={"x-forwarded-for": "8.8.8.8"}).json()
check("a new IP is still refused once the global cap is reached", r["ok"] is False)

client.__exit__(None, None, None)
print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
