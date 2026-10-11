"""
Offline checks (no LLM, no network) for the "fixed tools first, SQL as the
fallback" flow in app/agent.py: the front agent's prompt and tools, the
hand-over to the SQL agent, and the per-stage data budget.

    python -m evals.test_tools_first
"""
import os
import sqlite3
import tempfile
from pathlib import Path

for k in ("DATABASE_URL", "DATABASE_URL_RO"):
    os.environ[k] = ""  # SQLite mode; set before app modules read it
os.environ["LLM_REQUEST_TOKEN_LIMIT"] = "8000"

from app import agent, citations  # noqa: E402
from app import db as appdb  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


class FakeAgent:
    """Stands in for an agent executor: records that it ran, and can act as
    a front agent that calls database_query."""

    def __init__(self, output, handoff=False, lookups=0):
        self.output, self.handoff, self.lookups, self.calls = output, handoff, lookups, 0

    def invoke(self, payload, config=None):
        self.calls += 1
        agent._ANSWER.get()["tool_calls"] = self.lookups
        if self.handoff:
            agent._ANSWER.get()["handoff"] = True
        return {"output": self.output}


cat = Path(tempfile.mkdtemp()) / "catalog.db"
con = sqlite3.connect(cat)
con.executescript("""
CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, course_label TEXT, crn TEXT,
  section_name TEXT, instructor TEXT, enrollment_status TEXT, credit_hours TEXT, description TEXT,
  title_search TEXT, instructor_last TEXT, instructor_initial TEXT, scraped_at TEXT);
CREATE TABLE meetings (year INT, semester TEXT, subject TEXT, course_number TEXT, crn TEXT, meeting_type TEXT,
  days_of_week TEXT, start_time TEXT, end_time TEXT, building TEXT, room TEXT, instructor TEXT,
  start_min INT, end_min INT, is_online INT);
CREATE TABLE subjects (code TEXT, name TEXT, college_code TEXT, college TEXT, search_text TEXT);
CREATE TABLE academic_calendar (year INT, semester TEXT, event_date TEXT, event_end_date TEXT, title TEXT,
  category TEXT, raw_date TEXT);
INSERT INTO subjects VALUES ('CS', 'Computer Science', 'KP', 'Grainger College of Engineering',
  'cs computer science grainger college of engineering');
INSERT INTO sections VALUES
  (2026,'fall','CS','225','Data Structures','101','AL1','Beckman, M','A','4 hours.','Lists and trees.',
   'data structures','beckman','m','2026-09-01 10:00:00');
INSERT INTO meetings VALUES
  (2026,'fall','CS','225','101','Lecture','MWF','11:00AM','11:50AM','Foellinger Auditorium','AUD','Beckman, M',660,710,0);
INSERT INTO academic_calendar VALUES
  (2026,'fall','2026-10-16',NULL,'Deadline for UG to drop a full-semester course without a W','drop','Oct 16');
""")
con.commit()
con.close()

saved_path, saved_front, saved_flag = appdb.DB_PATH, agent.build_front_agent, os.environ.get("TOOLS_FIRST")
appdb.DB_PATH = cat
try:
    # ---- the front prompt and tools ------------------------------------------------------------
    front = agent.render_front_context()
    check("the front prompt declines with the same sentence as the SQL agent's",
          "I can only answer questions about UIUC" in front and agent._scope_rules() in agent.SYSTEM_CONTEXT)
    check("the front prompt carries the live DATA NOTES and no table schema",
          front.endswith(agent._data_notes()) and "TABLES\n" not in front and "sql_db_query" not in front)
    check("the front prompt is under half the SQL agent's",
          agent._est_tokens(front) * 2 < agent._est_tokens(agent.render_system_context()))

    tools = {t.name: t for t in agent._make_front_tools()}
    check("the front agent has the lookups and the hand-over, and no SQL tool",
          set(tools) == {"course_facts", "department_overview", "search_sections", "academic_calendar",
                         agent.HANDOFF_TOOL})

    def fresh(question):
        agent.new_query_log(question, question, front=True)

    fresh("who teaches cs225 this fall")
    secs = tools["search_sections"].invoke({"subject": "CS", "course_number": "225", "term": "fall 2026"})
    check("search_sections returns a named course's sections with CRN, instructor and meeting",
          "CRN 101" in secs and "Beckman, M" in secs and "MWF" in secs and not agent._handing_over())
    check("academic_calendar returns dated events",
          "2026-10-16" in tools["academic_calendar"].invoke({"category": "drop"}))

    fresh("the data structures class who teaches it")
    check("a course code the student never wrote hands over instead of being looked up",
          tools["search_sections"].invoke({"subject": "CS", "course_number": "225"}) == agent._HANDOFF_OUTPUT
          and agent._handing_over())
    fresh("should I take the data structures class")
    check("... for course_facts too",
          tools["course_facts"].invoke({"course": "CS 225"}) == agent._HANDOFF_OUTPUT and agent._handing_over())
    fresh("History: we talked about CS 225. Current question: who teaches it")
    check("a code written earlier in the conversation counts as written",
          "CRN 101" in tools["search_sections"].invoke({"subject": "CS", "course_number": "225"}))
    fresh("what does Prof Vishal teach")
    check("a lookup that finds nothing hands over (the SQL agent has the checks for why)",
          tools["search_sections"].invoke({"instructor": "Vishal"}) == agent._HANDOFF_OUTPUT
          and agent._handing_over())
    fresh("when does cs 225 meet")
    check("search_sections without a course or instructor hands over",
          tools["search_sections"].invoke({"subject": "CS"}) == agent._HANDOFF_OUTPUT)
    fresh("which CS 400-level courses are offered")
    check("a whole-department list asked for one level hands over",
          tools["department_overview"].invoke({"subjects": "CS"}) == agent._HANDOFF_OUTPUT)
    fresh("what does CS offer")
    check("a whole-department list is the direct answer",
          "CS 225" in tools["department_overview"].invoke({"subjects": "CS"})
          and "[CS 225](/?course=CS-225)" in agent._ANSWER.get()["direct"])

    agent.new_query_log("q", "q", front=True)
    out = tools[agent.HANDOFF_TOOL].invoke({"reason": "a count"})
    check("database_query marks the answer as handed over", agent._handed_over(out) and agent._handing_over())
    check("a lookup called beside database_query does no work",
          tools["academic_calendar"].invoke({}) == agent._HANDOFF_OUTPUT)
    fresh("who teaches CS 225")
    agent._ANSWER.get()["tool_calls"] = 1
    check("a front answer built on a lookup is not a hand-over", not agent._handed_over("Beckman, M teaches it."))
    fresh("what is the status breakdown for CS")
    check("a front answer with no lookup behind it hands over", agent._handed_over("Status isn't published yet."))
    decline = "I can only answer questions about UIUC course data. I can help with courses."
    fresh("what's the weather in Champaign")
    check("a plain decline stands", not agent._handed_over(decline))
    fresh("the ai class who teaches it")
    check("a decline of a question that mentions a course gets the SQL agent's second look",
          agent._handed_over(decline))
    fresh("can cs 412 count as grad credit later")
    check("... or names a course code", agent._handed_over(decline))
    check("running out of steps hands over", agent._handed_over(agent.AGENT_STOPPED_RAW))

    # ---- the per-stage data budget -------------------------------------------------------------
    agent._FRONT_TOOL_TEXT.update(text=" ".join(f"{t.name} {t.description}" for t in tools.values()),
                                  count=len(tools))
    agent.new_query_log("q", "q", front=True)
    front_room = agent._DATA_BUDGET.get()["left"]
    agent.new_query_log("q", "q")
    check("the front agent's shorter prompt leaves more room for data",
          front_room > agent._DATA_BUDGET.get()["left"] + 1000)

    # ---- run_agent: which stages run -----------------------------------------------------------
    sql_agent = FakeAgent("from sql")
    os.environ["TOOLS_FIRST"] = ""
    agent.build_front_agent = lambda **kw: (_ for _ in ()).throw(AssertionError("front agent built with the flag off"))
    check("flag off: only the SQL agent runs",
          agent.run_agent(sql_agent, "q", "q", []) == "from sql" and sql_agent.calls == 1)

    os.environ["TOOLS_FIRST"] = "1"
    answering = FakeAgent("from front", lookups=1)
    agent.build_front_agent = lambda **kw: answering
    sql_agent = FakeAgent("from sql")
    check("flag on: a front answer is the answer and the SQL agent never runs",
          agent.run_agent(sql_agent, "q", "q", []) == "from front" and sql_agent.calls == 0)

    handing = FakeAgent(agent._HANDOFF_OUTPUT, handoff=True)
    agent.build_front_agent = lambda **kw: handing
    check("flag on: a hand-over runs the SQL agent, with a fresh per-answer state",
          agent.run_agent(sql_agent, "q", "q", []) == "from sql" and handing.calls == 1
          and sql_agent.calls == 1 and not agent._handing_over())

    # ---- sources for the new lookups -----------------------------------------------------------
    cap = citations.SQLCapture()
    cap.on_tool_start({"name": "search_sections"}, "{'subject': 'CS', 'course_number': '225'}")
    cap.on_tool_start({"name": "academic_calendar"}, "{'category': 'drop'}")
    cap.on_tool_start({"name": agent.HANDOFF_TOOL}, "{'reason': 'select the count'}")
    tables = citations.tables_in(cap.source_sql)
    check("the Sources footer knows what the lookups read",
          {"sections", "meetings", "academic_calendar"} <= tables and not cap.queries)
finally:
    appdb.DB_PATH, agent.build_front_agent = saved_path, saved_front
    if saved_flag is None:
        os.environ.pop("TOOLS_FIRST", None)
    else:
        os.environ["TOOLS_FIRST"] = saved_flag

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
