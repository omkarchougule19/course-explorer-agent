"""
Offline checks for the query-gap fixes (2026-10-01): searchable name and
title fields (app/searchfields.py), the per-answer query checks in
app/agent.py (dropped filters, semester without year, sections/meetings
joins without crn), the taught_by lookup, and the longer latest answer in
the history block. No LLM, no network.

    python -m evals.test_search_fields
"""
import os
import sqlite3
import tempfile
from pathlib import Path

for k in ("DATABASE_URL", "DATABASE_URL_RO"):
    os.environ[k] = ""

from app import agent  # noqa: E402
from app import db as appdb  # noqa: E402
from app.searchfields import instructor_fields, is_online, title_search_text  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# -- name and title fields, several shapes each -------------------------------
check("plain 'Last, F'", instructor_fields("Fleck, M") == ("fleck", "m"))
check("apostrophe", instructor_fields("O'Brien, K") == ("obrien", "k"))
check("hyphenated", instructor_fields("Fagen-Ulmschneider, W") == ("fagenulmschneider", "w"))
check("multi-word last name", instructor_fields("Alves de Oliveira, R") == ("alvesdeoliveira", "r"))
check("lowercase initial", instructor_fields("Condon, e") == ("condon", "e"))
check("'-' as the initial", instructor_fields("Kevin Daniel, -") == ("kevindaniel", None))
check("unassigned", instructor_fields("-") == (None, None) and instructor_fields(None) == (None, None))
check("'&' becomes 'and'", "languages and compilers" in title_search_text("Programming Languages & Compilers"))
check("abbreviations expanded, originals kept",
      title_search_text("Intro Computing: Engrg & Sci") == "intro introduction computing engrg engineering and sci science")
check("ambiguous abbreviation left alone", title_search_text("Comp Methods") == "comp methods")
check("online variants", all(is_online(t) for t in ("Online", "Online Lecture", "Online Lab", "online discussion")))
check("not online", not any(is_online(t) for t in ("Lecture", "Lecture-Discussion", None)))

# -- question filters and the dropped-filter note ---------------------------
real_cov = agent._subject_coverage
agent._subject_coverage = lambda: ("fall 2026", {s: "fall 2026" for s in ("CS", "STAT", "ECE", "ART", "IS")})
try:
    check("code before a course number", agent._question_filters("can i take cs225 and STAT 400")[0] == ["CS", "STAT"])
    check("code before 'courses'", agent._question_filters("1 credit CS courses")[0] == ["CS"])
    check("code before 'classes', any case", agent._question_filters("art classes on fridays")[0] == ["ART"])
    check("English words that aren't codes are ignored",
          agent._question_filters("is it too late to add classes")[0] == [])
    check("level, two spellings", agent._question_filters("ECE 400-level electives")[1] == "4"
          and agent._question_filters("any 3xx stat classes")[1] == "3")

    agent.new_query_log("1 credit CS courses")
    note = agent._dropped_filter_note("SELECT DISTINCT subject, course_number FROM sections WHERE credit_min <= 1")
    check("a dropped subject gets a note", bool(note) and "subject CS" in note)
    check("a kept subject gets none",
          agent._dropped_filter_note("SELECT * FROM sections WHERE subject = 'CS' AND credit_min <= 1") is None)
    check("non-catalog tables are not checked",
          agent._dropped_filter_note("SELECT * FROM academic_calendar WHERE category = 'drop'") is None)
    agent.new_query_log("CS 400-level courses in fall 2026")
    check("a dropped level gets a note",
          "400-level" in (agent._dropped_filter_note("SELECT * FROM sections WHERE subject = 'CS'") or ""))
    check("a kept level gets none",
          agent._dropped_filter_note("SELECT * FROM sections WHERE subject = 'CS' AND course_number LIKE '4%'") is None)
    agent.new_query_log("")
    check("no question, no note", agent._dropped_filter_note("SELECT * FROM sections") is None)
finally:
    agent._subject_coverage = real_cov

# -- semester without year, joins without crn --------------------------------
check("semester alone gets a note", bool(agent._semester_note("SELECT * FROM sections WHERE semester = 'fall'")))
check("semester with year gets none",
      agent._semester_note("SELECT * FROM sections WHERE semester = 'fall' AND year = 2026") is None)
bad_join = ("SELECT m.start_time FROM sections s JOIN meetings m ON s.year = m.year AND s.semester = m.semester "
            "AND s.subject = m.subject AND s.course_number = m.course_number WHERE s.subject = 'CS'")
good_join = bad_join.replace("s.course_number = m.course_number", "s.course_number = m.course_number AND s.crn = m.crn")
exists_join = ("SELECT s.crn FROM sections s WHERE EXISTS (SELECT 1 FROM meetings m WHERE m.year = s.year AND "
               "m.semester = s.semester AND m.subject = s.subject AND m.course_number = s.course_number AND m.crn = s.crn)")
check("join without crn is rejected", bool(agent._join_issue(bad_join)))
check("join with crn passes", agent._join_issue(good_join) is None)
check("EXISTS with crn passes", agent._join_issue(exists_join) is None)
check("one table alone is not checked", agent._join_issue("SELECT * FROM meetings WHERE start_min >= 720") is None)

# -- taught_by lookup on a throwaway catalog ---------------------------------
cat = Path(tempfile.mkdtemp()) / "catalog.db"
con = sqlite3.connect(cat)
con.executescript("""
CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, instructor TEXT,
  instructor_last TEXT, instructor_initial TEXT);
INSERT INTO sections VALUES
  (2026, 'fall', 'CS', '341', 'Angrave, L', 'angrave', 'l'),
  (2026, 'fall', 'CS', '199', 'Angrave, L', 'angrave', 'l'),
  (2026, 'fall', 'MATH', '101', 'OBrien, K', 'obrien', 'k'),
  (2026, 'fall', 'ADV', '281', 'Ji, H', 'ji', 'h'),
  (2026, 'fall', 'CS', '107', 'Ji, E', 'ji', 'e');
""")
con.commit()
con.close()
saved = appdb.DB_PATH
appdb.DB_PATH = cat
try:
    c = appdb.get_connection()
    found, label = agent._courses_taught_by(c, "Lawrence Angrave")
    check("full name -> that instructor's courses", sorted(found) == [("CS", "199"), ("CS", "341")])
    check("the label carries the initial caveat", "may be a different person" in label)
    check("last name only matches every initial", len(agent._courses_taught_by(c, "Ji")[0]) == 2)
    check("first name narrows by initial", agent._courses_taught_by(c, "Heng Ji")[0] == [("ADV", "281")])
    check("apostrophe and comma forms", agent._courses_taught_by(c, "O'Brien, K")[0] == [("MATH", "101")])
    found, label = agent._courses_taught_by(c, "Nobody Here")
    check("no match says so", found == [] and "No instructor matching" in label)
    c.close()
finally:
    appdb.DB_PATH = saved

# -- name literals and time comparisons -------------------------------------
check("hyphen and case removed from an instructor_last literal",
      agent._normalize_name_literals("WHERE instructor_last = 'Fagen-Ulmschneider' AND instructor_initial = 'W'")
      == "WHERE instructor_last = 'fagenulmschneider' AND instructor_initial = 'w'")
check("LIKE wildcards kept", agent._normalize_name_literals("WHERE instructor_last LIKE '%O''Brien%'")
      .startswith("WHERE instructor_last LIKE '%o"))
check("other literals untouched", agent._normalize_name_literals("WHERE subject = 'CS'") == "WHERE subject = 'CS'")
check("text time comparison is caught", bool(agent._TIME_TEXT_CMP_RE.search("WHERE m.start_time > '02:00 PM'")))
check("minute comparison is fine", not agent._TIME_TEXT_CMP_RE.search("WHERE m.start_min >= 840"))
check("selecting the time text is fine", not agent._TIME_TEXT_CMP_RE.search("SELECT m.start_time, m.end_time FROM meetings m"))

# -- empty multi-word title searches ----------------------------------------
n = agent._title_note("SELECT * FROM sections WHERE title_search LIKE '%intro psychology%'")
check("a phrase gets split into words", bool(n) and "title_search LIKE '%intro%' AND title_search LIKE '%psychology%'" in n)
check("a single word gets no title note", agent._title_note("SELECT * FROM sections WHERE title_search LIKE '%psych%'") is None)

# -- department words and invented codes in the topic search -----------------
cat2 = Path(tempfile.mkdtemp()) / "catalog2.db"
con = sqlite3.connect(cat2)
con.executescript("""
CREATE TABLE subjects (code TEXT PRIMARY KEY, name TEXT, college_code TEXT, college TEXT, search_text TEXT);
INSERT INTO subjects VALUES
  ('ANSC', 'Animal Sciences', 'KL', 'ACES', 'ansc animal sciences college of agricultural, consumer and environmental sciences (aces, agriculture)'),
  ('ACE', 'Agricultural and Consumer Economics', 'KL', 'ACES', 'ace agricultural and consumer economics college of agricultural ... (aces, agriculture)'),
  ('FIN', 'Finance', 'KM', 'Gies', 'fin finance gies college of business'),
  ('CS', 'Computer Science', 'KP', 'Grainger', 'cs computer science grainger college of engineering');
""")
con.commit(); con.close()
saved = appdb.DB_PATH
appdb.DB_PATH = cat2
try:
    c = appdb.get_connection()
    check("real codes pass through", agent._resolve_subjects(c, "CS") == (["CS"], []))
    check("a college word resolves to its departments", agent._resolve_subjects(c, "agriculture")[0] == ["ACE", "ANSC"])
    check("a college name resolves", agent._resolve_subjects(c, "gies") == (["FIN"], []))
    codes, bad = agent._resolve_subjects(c, "AGRI, CULT, URE")
    check("invented codes are resolved by stem or reported", "AGRI" not in codes and set(bad) <= {"AGRI", "CULT", "URE"})
    check("empty input", agent._resolve_subjects(c, "") == ([], []))
    c.close()
finally:
    appdb.DB_PATH = saved

# -- latest-term rerun for a subject not synced for the latest term ----------
cat3 = Path(tempfile.mkdtemp()) / "catalog3.db"
con = sqlite3.connect(cat3)
con.executescript("""
CREATE TABLE sections (year INT, semester TEXT, subject TEXT, course_number TEXT, course_label TEXT);
INSERT INTO sections VALUES (2026, 'fall', 'CS', '225', 'Data Structures'),
  (2026, 'spring', 'PSYC', '100', 'Intro Psych'), (2026, 'spring', 'PSYC', '238', 'Psychopathology');
""")
con.commit(); con.close()
real_cov = agent._subject_coverage
agent._subject_coverage = lambda: ("fall 2026", {"CS": "fall 2026", "PSYC": "spring 2026"})
try:
    d = agent._CappedSQLDatabase.from_uri(f"sqlite:///{cat3.as_posix()}")
    out = d.run("SELECT course_number FROM sections WHERE subject = 'PSYC' AND year = 2026 AND semester = 'fall'")
    check("an unsynced subject's empty result is rerun for its latest term",
          "query for spring 2026 returned" in out and "100" in out and "238" in out)
    check("a synced subject's empty result is left alone",
          "latest term" not in (d.run("SELECT course_number FROM sections WHERE subject = 'CS' AND year = 2026 "
                                      "AND semester = 'fall' AND course_number = '999'") or ""))
finally:
    agent._subject_coverage = real_cov

# -- history: the latest answer survives for refinements ---------------------
long = "CS 400 ... " * 100
hist = agent._format_history([{"q": "a", "a": long}, {"q": "b", "a": long}])
first, last = [l for l in hist.splitlines() if l.startswith("Assistant:")]
check("older answers stay short, the latest is kept long", len(first) < 300 and len(last) > 1000)

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
