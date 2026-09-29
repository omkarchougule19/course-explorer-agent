"""
Offline checks for the course_facts tool (app/agent.py course_facts_text) and
its Sources-footer wiring (app/citations.py SQLCapture). No LLM, no network:
a throwaway SQLite catalog with the columns the tool reads.

    python -m evals.test_course_facts
"""
import os
import sqlite3
import tempfile
from pathlib import Path

for k in ("DATABASE_URL", "DATABASE_URL_RO"):
    os.environ[k] = ""  # SQLite mode; set before app modules read it

from app import agent, citations  # noqa: E402
from app import db as appdb  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


cat = Path(tempfile.mkdtemp()) / "catalog.db"
con = sqlite3.connect(cat)
con.executescript("""
CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, crn TEXT,
  instructor TEXT, enrollment_status TEXT, course_label TEXT, credit_hours TEXT, description TEXT);
CREATE TABLE prerequisites (subject TEXT, course_number TEXT, group_index INT, req_subject TEXT,
  req_course_number TEXT, relation TEXT, condition_text TEXT, raw_text TEXT);
CREATE TABLE gen_ed_categories (subject TEXT, course_number TEXT, course_title TEXT,
  acp TEXT, cs TEXT, hum TEXT, nat TEXT, qr TEXT, sbs TEXT);
CREATE TABLE grade_distributions (year INT, subject TEXT, course_number TEXT, primary_instructor TEXT,
  a_plus INT, a INT, a_minus INT, students INT);
CREATE TABLE teachers_ranked_excellent (year INT, term TEXT, last_name TEXT, first_name TEXT);
CREATE TABLE academic_calendar (year INT, semester TEXT, event_date TEXT, category TEXT, title TEXT);
INSERT INTO academic_calendar VALUES
  (2026, 'spring', '2026-03-01', 'drop', 'Old spring deadline'),
  (2026, 'fall', '2026-10-16', 'withdraw', 'Deadline for UG to withdraw without W'),
  (2026, 'fall', '2026-09-04', 'add', 'Add deadline');
INSERT INTO sections VALUES
  (2026, 'fall',   'CS', '444', '1', 'Gupta, S',    'A',      'Deep Learning for Computer Vision', '3 OR 4 hours.', 'Neural networks.'),
  (2026, 'fall',   'CS', '444', '2', 'Gupta, S',    'P',      'Deep Learning for Computer Vision', '3 OR 4 hours.', 'Neural networks.'),
  (2026, 'spring', 'CS', '444', '3', 'Lazebnik, S', 'Closed', 'Old Title',                         '3 hours',       NULL),
  (2026, 'spring', 'CS', '444', '4', NULL,          'Closed', 'Old Title',                         '3 hours',       NULL),
  (2025, 'fall',   'CS', '444', '5', 'Lazebnik, S', 'Open',   'Old Title',                         '3 hours',       NULL),
  (2025, 'spring', 'CS', '444', '6', 'Lazebnik, S', 'Open',   'Old Title',                         '3 hours',       NULL);
INSERT INTO prerequisites VALUES
  ('CS', '444', 0, 'MATH', '241', 'prereq', NULL, 'raw'),
  ('CS', '444', 1, 'MATH', '257', 'prereq', NULL, 'raw'),
  ('CS', '444', 1, 'MATH', '415', 'prereq', NULL, 'raw'),
  ('CS', '444', 2, 'CS',   '225', 'concurrent', NULL, 'raw'),
  ('CS', '444', 3, NULL,   NULL,  'prereq', 'junior standing', 'raw');
INSERT INTO gen_ed_categories VALUES ('CS', '444', 'Deep Learning', NULL, NULL, NULL, NULL, 'QR2', NULL);
INSERT INTO grade_distributions VALUES (2024, 'CS', '444', 'Lazebnik, S', 10, 20, 10, 80);
INSERT INTO teachers_ranked_excellent VALUES (2024, 'fall', 'Lazebnik', 'Svetlana');
""")
con.commit()
con.close()

saved = appdb.DB_PATH
appdb.DB_PATH = cat
try:
    out = agent.course_facts_text("cs444")
    print(out, "\n")
    check("title comes from the newest term's course_label",
          out.startswith("CS 444: Deep Learning for Computer Vision"))
    check("credits from the newest term", "Credits: 3 OR 4 hours." in out)
    check("alternatives in one group are joined by 'or'", "MATH 257 or MATH 415" in out)
    check("groups are separate required lines", "MATH 241; MATH 257 or MATH 415; CS 225" in out)
    check("concurrent enrollment is marked", "CS 225 (may be taken concurrently)" in out)
    check("a non-course condition is kept", "junior standing" in out)
    check("gen-ed codes listed", "Gen-eds: QR2" in out)
    check("A/P codes read as registration not published",
          "fall 2026: 2 section(s); instructors: Gupta, S; status: registration not published yet" in out)
    check("closed sections counted", "spring 2026: 2 section(s); instructors: Lazebnik, S; status: 2 Closed" in out)
    check("only the last 3 terms listed", "spring 2025" not in out
          and "(also offered in 1 earlier term(s) in the data)" in out)
    check("grades summarized when loaded", "Lazebnik, S 2024: 50% A-range of 80 students" in out)
    check("Ranked Excellent matched on last name + initial", "Ranked Excellent by students: Lazebnik, S (fall 2024)" in out)
    check("says what the data can't tell", out.rstrip().endswith("future offerings or seats."))
    check("unknown course says so", agent.course_facts_text("CS 999").startswith("No course CS 999 in the data"))
    check("no course code asks for one", agent.course_facts_text("deep learning") == "Give one course code, e.g. 'CS 444'.")
    check("stays compact", len(out) < 1500)
    chk = agent.course_facts_text("CS 444", completed="cs225, MATH 241")
    check("eligibility: met lines named", "Met: MATH 241; CS 225." in chk)
    check("eligibility: missing line with its alternatives", "Missing: MATH 257 or MATH 415." in chk)
    check("eligibility: says plainly it isn't met yet", "NOT yet eligible - 1 line(s) still missing" in chk)
    check("eligibility: a non-course condition is flagged, not judged",
          "Not checkable from the data: junior standing." in chk)
    all_met = agent.course_facts_text("CS 444", completed="MATH 241, MATH 415, CS 225")
    check("eligibility: all course lines met", "all listed course prerequisites met" in all_met)
    check("no completed list, no check line", "Check against" not in out)
    check("latest term's drop/withdraw deadlines, not add dates or older terms",
          "Drop/withdraw deadlines, fall 2026: 2026-10-16 Deadline for UG to withdraw without W" in out
          and "Old spring" not in out and "Add deadline" not in out)
finally:
    appdb.DB_PATH = saved

cap = citations.SQLCapture()
cap.on_tool_start({"name": "course_facts"}, "{'course': 'cs 444'}")
check("course_facts is not recorded as agent SQL (evals score that)", cap.queries == [])
check("but the Sources footer sees its tables and subject",
      citations.tables_in(cap.source_sql) == {"sections", "prerequisites"}
      and citations._subjects_in(cap.source_sql) == {"CS"})
check("the tool is registered with a status label", "course_facts" in agent._TOOL_LABELS)

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
