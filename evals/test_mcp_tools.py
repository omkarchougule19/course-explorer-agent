"""
Offline checks (no LLM, no network, no hosted database) for the lookups in
app/catalog.py behind the MCP tools, and for the MCP endpoint on the web
service, on a throwaway SQLite catalog.

    python -m evals.test_mcp_tools
"""
import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

for k in ("DATABASE_URL", "DATABASE_URL_RO", "PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
    os.environ[k] = ""          # SQLite mode; set before the app modules read it
os.environ["MCP_RATE_PER_MINUTE"] = "5"

from app import catalog  # noqa: E402
from app import db as appdb  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


cat = Path(tempfile.mkdtemp()) / "catalog.db"
con = sqlite3.connect(cat)
con.executescript("""
CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, course_label TEXT, crn TEXT,
  section_name TEXT, instructor TEXT, enrollment_status TEXT, credit_hours TEXT, description TEXT,
  title_search TEXT, instructor_last TEXT, instructor_initial TEXT, grad_credit TEXT, grad_min REAL,
  grad_max REAL, restriction TEXT, scraped_at TEXT);
CREATE TABLE meetings (year INT, semester TEXT, subject TEXT, course_number TEXT, crn TEXT, meeting_type TEXT,
  days_of_week TEXT, start_time TEXT, end_time TEXT, building TEXT, room TEXT, instructor TEXT,
  start_min INT, end_min INT, is_online INT);
CREATE TABLE subjects (code TEXT, name TEXT, college_code TEXT, college TEXT, search_text TEXT);
CREATE TABLE prerequisites (subject TEXT, course_number TEXT, group_index INT, req_subject TEXT,
  req_course_number TEXT, relation TEXT, condition_text TEXT, raw_text TEXT);
CREATE TABLE gen_ed_categories (subject TEXT, course_number TEXT, course_title TEXT,
  acp TEXT, cs TEXT, hum TEXT, nat TEXT, qr TEXT, sbs TEXT);
CREATE TABLE grade_distributions (year INT, term TEXT, year_term TEXT, sched_type TEXT, subject TEXT,
  course_number TEXT, primary_instructor TEXT, a_plus INT, a INT, a_minus INT, b_plus INT, b INT, b_minus INT,
  c_plus INT, c INT, c_minus INT, d_plus INT, d INT, d_minus INT, f INT, w INT, students INT);
CREATE TABLE teachers_ranked_excellent (year INT, term TEXT, last_name TEXT, first_name TEXT);
CREATE TABLE academic_calendar (year INT, semester TEXT, event_date TEXT, event_end_date TEXT, title TEXT,
  category TEXT, raw_date TEXT);
INSERT INTO subjects VALUES
  ('CS', 'Computer Science', 'KP', 'Grainger College of Engineering', 'cs computer science grainger college of engineering'),
  ('BADM', 'Business Administration', 'KM', 'Gies College of Business', 'badm business administration gies college of business'),
  ('STAT', 'Statistics', 'KV', 'Liberal Arts and Sciences', 'stat statistics liberal arts and sciences');
INSERT INTO sections VALUES
  (2026,'fall','CS','225','Data Structures','101','AL1','Beckman, M','A','4 hours.','Lists, stacks, queues and trees.','data structures','beckman','m','no',NULL,NULL,NULL,'2026-09-01 10:00:00'),
  (2026,'fall','CS','225','Data Structures','102','AYA',NULL,'A','4 hours.','Lists, stacks, queues and trees.','data structures',NULL,NULL,'no',NULL,NULL,NULL,'2026-09-01 10:00:00'),
  (2026,'fall','CS','441','Applied Machine Learning','201','A','Hoiem, D','A','3 OR 4 hours.','Techniques of machine learning for signal problems.','applied machine learning','hoiem','d','yes',4,4,NULL,'2026-09-01 10:00:00'),
  (2026,'spring','CS','225','Data Structures','301','AL1','Solomon, B','Open','4 hours.','Lists, stacks, queues and trees.','data structures','solomon','b','no',NULL,NULL,NULL,'2026-02-01 10:00:00'),
  (2026,'spring','BADM','210','Business Analytics I','401','A','Larson, E','Open','3 hours.','Analytics for business decisions.','business analytics i','larson','e','no',NULL,NULL,NULL,'2026-02-01 10:00:00');
INSERT INTO meetings VALUES
  (2026,'fall','CS','225','101','Lecture','MWF','11:00AM','11:50AM','Foellinger Auditorium','AUD','Beckman, M',660,710,0),
  (2026,'fall','CS','225','102','Laboratory-Discussion','R','08:00AM','09:50AM','Siebel Center','0218',NULL,480,590,0),
  (2026,'fall','CS','441','201','Online Lecture','TR','02:00PM','03:15PM',NULL,NULL,'Hoiem, D',840,915,1);
INSERT INTO prerequisites VALUES
  ('CS','225',0,'CS','128','prereq',NULL,'raw'), ('CS','225',0,'ECE','220','prereq',NULL,'raw'),
  ('CS','225',1,'CS','173','prereq',NULL,'raw');
INSERT INTO academic_calendar VALUES
  (2026,'fall','2026-08-24',NULL,'First day of instruction','instruction','Aug 24'),
  (2026,'fall','2026-10-16',NULL,'Deadline for UG to drop a full-semester course without a W','drop','Oct 16'),
  (2026,'spring','2026-03-13',NULL,'Old spring drop deadline','drop','Mar 13');
""")
con.commit()
con.close()

saved = appdb.DB_PATH
appdb.DB_PATH = cat
try:
    facts = catalog.course_facts_text("cs225", completed="CS 128")
    check("course facts: title, 'or' alternatives and the eligibility check",
          facts.startswith("CS 225: Data Structures") and "CS 128 or ECE 220" in facts
          and "NOT yet eligible - 1 line(s) still missing" in facts)
    check("course facts: unknown course says so", catalog.course_facts_text("CS 999").startswith("No course CS 999"))

    check("department overview: site-relative links for the site's own assistant",
          "| [CS 225](/?course=CS-225) | Data Structures | 2 |" in catalog.department_overview_text("CS"))
    check("department overview: absolute links when given the site's address",
          "(https://example.test/?course=CS-225)" in catalog.department_overview_text("CS", base="https://example.test"))
    os.environ["RENDER_EXTERNAL_URL"] = "https://example.test/"
    check("the site's address falls back to the one the host provides", catalog.site_url() == "https://example.test")
    check("department overview: a college word finds its departments",
          "BADM (Business Administration)" in catalog.department_overview_text("Gies"))

    secs = catalog.search_sections_text(subject="CS", course_number="225", term="fall 2026")
    check("sections: one line per section with CRN, instructor and meeting",
          secs.startswith("2 section(s) in fall 2026") and "CRN 101 AL1 | Beckman, M | A | Lecture MWF 11:00AM-11:50AM"
          in secs and "instructor not assigned" in secs)
    check("sections: the status-code note is added when codes are shown", "scheduling code" in secs)
    latest = catalog.search_sections_text(subject="CS")
    check("sections: no term means the subject's latest term, and says so",
          "showing the latest term in the data, fall 2026" in latest and "CRN 301" not in latest)
    no8 = catalog.search_sections_text(subject="CS", term="fall 2026", starts_after="09:00")
    check("sections: 'no 8 AMs' drops the section that meets at 8", "CRN 102" not in no8 and "CRN 101" in no8)
    nofri = catalog.search_sections_text(subject="CS", term="fall 2026", no_days="F")
    check("sections: 'no Fridays' drops the MWF lecture", "CRN 101" not in nofri and "CRN 102" in nofri)
    online = catalog.search_sections_text(subject="CS", term="fall 2026", online_only=True)
    check("sections: online only", "CRN 201" in online and "CRN 101" not in online)
    check("sections: a level filter", "CS 441" in catalog.search_sections_text(subject="CS", term="fall 2026", level="4")
          and "CS 225" not in catalog.search_sections_text(subject="CS", term="fall 2026", level="400-level"))
    by_name = catalog.search_sections_text(instructor="Solomon", term="spring 2026")
    check("sections: by instructor last name, no subject needed", "CRN 301" in by_name and "Solomon, B" in by_name)
    check("sections: a term with no rows names the latest term that has some",
          "CS's latest term in the data is fall 2026" in catalog.search_sections_text(subject="CS", term="fall 2027"))
    check("sections: a mistyped department gets the closest code",
          "Closest: BADM (Business Administration)" in catalog.search_sections_text(subject="BAD"))
    check("sections: nothing to search asks for a subject or instructor",
          catalog.search_sections_text().startswith("Give a subject code"))

    found = catalog.search_courses_text("machine learning")
    check("courses: every word must match; title and description are searched",
          found.startswith("1 course(s) match") and "CS 441 - Applied Machine Learning" in found)
    check("courses: one row per course, not per section",
          catalog.search_courses_text("data structures").startswith("1 course(s) match"))
    check("courses: no match says so", catalog.search_courses_text("underwater basket").startswith("No course title"))
    check("courses: an empty query asks for words", catalog.search_courses_text("  ").startswith("Give one or more words"))

    cal = catalog.calendar_text()
    check("calendar: the latest term by default", cal.startswith("Academic calendar, fall 2026 (2 event(s))")
          and "Old spring" not in cal)
    check("calendar: one category", "First day" not in catalog.calendar_text(category="drop")
          and "2026-10-16 [drop]" in catalog.calendar_text(category="drop"))
    check("calendar: a named term", "Old spring drop deadline" in catalog.calendar_text("spring 2026"))

    check("departments: search by a word of the college", catalog.departments_text("gies").startswith("1 department(s)"))
    check("departments: a wrong word offers the closest code",
          "Closest codes: STAT (Statistics)" in catalog.departments_text("stats")
          or catalog.departments_text("stats").startswith("1 department(s)"))

    # ---- the MCP endpoint on the web service ---------------------------------------------------
    from fastapi.testclient import TestClient
    from app import api
    hdr = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    with TestClient(api.app) as client:
        info = client.get("/mcp-info").json()
        check("site: /mcp-info gives the MCP address and the six tools",
              info["url"] == "https://example.test/mcp" and len(info["tools"]) == 6)
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=hdr)
        tools = r.json().get("result", {}).get("tools", [])
        check("mcp: the endpoint answers tools/list with the six tools", r.status_code == 200 and len(tools) == 6)
        check("mcp: no tool takes SQL", not any("sql" in p.lower() for tl in tools
                                                for p in tl.get("inputSchema", {}).get("properties", {})))
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "department_overview", "arguments": {"subjects": "CS"}}}
        r = client.post("/mcp", json=call, headers=hdr)
        body = r.json().get("result", {}).get("content", [{}])[0].get("text", "")
        check("mcp: a tool call over HTTP returns the data, with absolute course links",
              "(https://example.test/?course=CS-225)" in body)
        codes = [client.post("/mcp", json=call, headers=hdr).status_code for _ in range(6)]
        check("mcp: an address over the per-minute limit gets 429", 429 in codes)
        check("mcp: the limit leaves the rest of the site alone", client.get("/mcp-info").status_code == 200)

    from app.mcp_server import mcp
    out = asyncio.run(mcp.call_tool("course_facts", {"course": "CS 225"}))
    text = "".join(getattr(b, "text", "") for b in getattr(out, "content", []))
    check("mcp: calling a tool returns its text", text.startswith("CS 225: Data Structures"))
finally:
    appdb.DB_PATH = saved

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
