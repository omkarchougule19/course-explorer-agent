"""
Offline checks for whole-department questions ("sections under badm", "what
does Gies offer"): the department_overview tool (app/agent.py
department_overview_text), the closest-code suggestion for a mistyped
department, and the Sources-footer wiring. No LLM, no network: a throwaway
SQLite catalog with the columns the tool reads.

    python -m evals.test_department_overview
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
  course_label TEXT);
CREATE TABLE subjects (code TEXT, name TEXT, college_code TEXT, college TEXT, search_text TEXT);
INSERT INTO subjects VALUES
  ('BADM', 'Business Administration', 'KM', 'Gies College of Business',
   'badm business administration gies college of business'),
  ('BDI', 'Business Data and Innovation', 'KM', 'Gies College of Business',
   'bdi business data and innovation gies college of business'),
  ('FIN', 'Finance', 'KM', 'Gies College of Business', 'fin finance gies college of business'),
  ('PSYC', 'Psychology', 'KV', 'Liberal Arts and Sciences', 'psyc psychology liberal arts and sciences'),
  ('CS', 'Computer Science', 'KP', 'Grainger College of Engineering',
   'cs computer science grainger college of engineering');
INSERT INTO sections VALUES
  (2026, 'fall',   'BADM', '210', '1', 'Business Analytics I'),
  (2026, 'fall',   'BADM', '210', '2', 'Business Analytics I'),
  (2026, 'fall',   'BADM', '554', '3', 'Enterprise Database Management'),
  (2026, 'spring', 'BADM', '210', '4', 'Business Analytics I'),
  (2026, 'spring', 'BADM', '320', '5', 'Principles of Marketing'),
  (2026, 'fall',   'BDI',  '513', '6', 'Data Storytelling'),
  (2026, 'spring', 'FIN',  '221', '7', 'Corporate Finance');
""")
con.executemany("INSERT INTO sections VALUES (2026, 'fall', 'CS', ?, ?, ?)",
                [(str(100 + i), str(100 + i), f"Course {i}") for i in range(agent._OVERVIEW_LIST_MAX + 5)])
con.commit()
con.close()

saved = appdb.DB_PATH
appdb.DB_PATH = cat
try:
    out = agent.department_overview_text("badm")
    print(out, "\n")
    check("opens with the department, its college, its latest term and the totals",
          out.startswith("**BADM (Business Administration)**, Gies College of Business: "
                         "3 sections across 2 courses in fall 2026."))
    check("each course is a linked table row with its number of sections",
          "| [BADM 210](/?course=BADM-210) | Business Analytics I | 2 |" in out
          and "| [BADM 554](/?course=BADM-554) | Enterprise Database Management | 1 |" in out)
    check("other terms in the data are named", "Also in the data: spring 2026 (2 sections)." in out)
    check("ends by offering a course's sections, with the largest course as the example",
          out.endswith('for example "BADM 210 sections".'))
    check("it is written for the student: no notes to the model", "[" + "Check" not in out and "call this" not in out)

    spring = agent.department_overview_text("BADM", term="Spring 2026")
    check("a named term is used", "2 sections across 2 courses in spring 2026" in spring and "BADM 320" in spring)
    missing = agent.department_overview_text("BADM", term="spring 2027")
    check("a term with no rows says so and gives the latest term",
          "has no spring 2027 schedule in the data yet. Its latest term is fall 2026" in missing)
    level = agent.department_overview_text("BADM", level="500-level")
    check("a level narrows the list", "1 section across 1 500-level course in fall 2026" in level and "BADM 210" not in level)
    check("a level with nothing gives the no-data sentence",
          agent.department_overview_text("BADM", level="1").startswith("There's no data for that in this dataset yet."))

    gies = agent.department_overview_text("Gies")
    check("a college word becomes every department in it, each listed",
          all(f"**{c} (" in gies for c in ("BADM", "BDI", "FIN")))
    check("each department uses its own latest term", "1 section across 1 course in spring 2026" in gies)

    big = agent.department_overview_text("CS")
    check("one department is listed in full however large",
          big.count("| [CS ") == agent._OVERVIEW_LIST_MAX + 5)
    many = agent.department_overview_text("CS,BADM")
    check("several departments over the limit: a row of totals each, no course list",
          "too many to list at once" in many and "| [CS " not in many
          and f"| CS (Computer Science) | fall 2026 | {agent._OVERVIEW_LIST_MAX + 5} | {agent._OVERVIEW_LIST_MAX + 5} |" in many
          and "| BADM (Business Administration) | fall 2026 | 2 | 3 |" in many)
    check("... and it says how to narrow", 'for example "CS courses" or "CS 400-level courses".' in many)

    typo = agent.department_overview_text("BDM")
    check("a mistyped code asks about the closest real one",
          typo == 'I couldn\'t find a department called "BDM". Did you mean BADM (Business Administration)?')
    check("nothing close: says how to name one",
          agent.department_overview_text("xyzzy").startswith('I couldn\'t find a department called "xyzzy". Try'))
    check("no department asks for one", agent.department_overview_text("").startswith("Which department?"))
    check("the tool hands its text straight to the student (no second model call)",
          agent._make_department_overview_tool().return_direct is True)
    check("a finished answer passes through the answer cleanup unchanged", agent.tidy_answer(out) == out)

    # closest-code suggestions, also used for an empty SQL result
    check("a code that starts a real one", agent._close_subjects("BAD")[0] == "BADM (Business Administration)")
    check("transposed letters", "PSYC (Psychology)" in agent._close_subjects("PYSC"))
    check("a real code followed by a letter", agent._close_subjects("CSE")[0] == "CS (Computer Science)")
    note = agent._unknown_subject_note("SELECT * FROM sections WHERE subject = 'BAD' AND year = 2026")
    check("empty result on an unknown code names the closest", bool(note) and "'BAD' is not a department code" in note
          and "BADM (Business Administration)" in note)
    check("a real code gets no note",
          agent._unknown_subject_note("SELECT * FROM sections WHERE subject = 'BADM'") is None)
    check("a query without a subject gets no note", agent._unknown_subject_note("SELECT 1 FROM sections") is None)
    note = agent._unknown_subject_note("SELECT * FROM sections WHERE subject IN ('BADM', 'BDM', 'PYSC')")
    check("every unknown code in an IN list is named, each with its closest match",
          bool(note) and "'BDM' is not a department code. Closest: BADM" in note
          and "'PYSC' is not a department code. Closest: PSYC" in note and "'BADM' is not" not in note)
    cap2 = citations.SQLCapture()
    cap2.on_tool_start({"name": "sql_db_query"}, "SELECT 1 FROM sections WHERE subject IN ('BADM', 'FIN')")
    check("the Sources footer sees every subject of an IN list",
          citations._subjects_in(cap2.source_sql) == {"BADM", "FIN"})
    # a cut-off list of a whole department's sections points at the tool
    cut = "[('1',)]\n[Result truncated: 9,000 characters returned, only the first 900 shown.]"
    whole = "SELECT year, semester, crn, instructor FROM sections WHERE subject = 'BADM' ORDER BY year DESC, crn"
    check("a truncated whole-department listing is steered to department_overview",
          "department_overview(subjects='BADM')" in (agent._whole_department_note(whole, cut) or ""))
    check("... not when the result was complete", agent._whole_department_note(whole, "[('1',)]") is None)
    check("... nor when the query is narrower (one course)", agent._whole_department_note(
        "SELECT crn FROM sections WHERE subject = 'BADM' AND course_number = '210'", cut) is None)
    check("... or an instructor", agent._whole_department_note(
        "SELECT crn FROM sections WHERE subject = 'BADM' AND instructor_last = 'larson'", cut) is None)
    check("... or a grouped count", agent._whole_department_note(
        "SELECT course_number, COUNT(*) FROM sections WHERE subject = 'BADM' GROUP BY course_number", cut) is None)
finally:
    appdb.DB_PATH = saved

cap = citations.SQLCapture()
cap.on_tool_start({"name": "department_overview"}, "{'subjects': 'BADM', 'term': 'fall 2026'}")
check("department_overview is not recorded as agent SQL (evals score that)", cap.queries == [])
check("but the Sources footer sees the schedule and the subject",
      citations.tables_in(cap.source_sql) == {"sections"} and "BADM" in citations._subjects_in(cap.source_sql))
check("the tool is registered with a status label", "department_overview" in agent._TOOL_LABELS)

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
