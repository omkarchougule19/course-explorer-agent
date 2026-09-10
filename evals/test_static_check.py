"""
test_static_check.py

Standalone checks for critique.static_schema_check - the deterministic half
of the Critic, and the thing the "hallucinated-reference rate" metric is
computed with. No pytest needed:

    .venv/Scripts/python -m evals.test_static_check

The point is to prove two properties: (1) valid SQL against the real schema
is never flagged (zero false positives), including the awkward cases -
aliases, CTEs, output aliases, subqueries; (2) genuine hallucinated
table/column references are caught.
"""

from app.sql_pipeline.critique import static_schema_check

# A trimmed stand-in for the live catalog - the columns the test SQL uses.
CATALOG = {
    "sections": {"year", "semester", "subject", "course_number", "course_label",
                 "crn", "section_name", "instructor", "enrollment_status",
                 "credit_hours", "description"},
    "meetings": {"year", "semester", "subject", "course_number", "crn",
                 "meeting_type", "days_of_week", "start_time", "end_time",
                 "building", "room", "instructor"},
    "gen_ed_categories": {"snapshot_year", "snapshot_term", "subject",
                          "course_number", "course_title", "acp", "cs", "hum",
                          "nat", "qr", "sbs"},
}

DIALECT = "sqlite"

# (name, sql, expect_ok)
CASES = [
    ("plain select", "SELECT instructor FROM sections WHERE subject = 'CS'", True),
    ("qualified + alias",
     "SELECT s.instructor FROM sections AS s WHERE s.course_number = '225'", True),
    ("join with aliases",
     "SELECT s.crn, m.start_time FROM sections s "
     "JOIN meetings m ON s.crn = m.crn WHERE s.subject = 'CS'", True),
    ("output alias in ORDER BY",
     "SELECT subject, COUNT(*) AS n FROM sections GROUP BY subject ORDER BY n DESC LIMIT 5", True),
    ("CTE",
     "WITH cs AS (SELECT * FROM sections WHERE subject = 'CS') "
     "SELECT course_number FROM cs LIMIT 10", True),
    ("subquery",
     "SELECT subject FROM sections WHERE crn IN "
     "(SELECT crn FROM meetings WHERE building = 'Siebel') LIMIT 10", True),
    ("aggregate no alias", "SELECT COUNT(*) FROM sections WHERE semester = 'fall'", True),
    # -- hallucinations that must be caught --
    ("nonexistent column (qualified)",
     "SELECT s.professor FROM sections s WHERE s.subject = 'CS'", False),
    ("nonexistent column (unqualified)",
     "SELECT rating FROM sections WHERE subject = 'CS'", False),
    ("nonexistent table",
     "SELECT * FROM course_reviews WHERE subject = 'CS'", False),
    ("real table, wrong column on join",
     "SELECT m.gpa FROM meetings m WHERE m.subject = 'CS'", False),
    ("unparseable", "SELECT FROM WHERE ORDER", False),
]


def main() -> int:
    failures = 0
    for name, sql, expect_ok in CASES:
        res = static_schema_check(sql, CATALOG, DIALECT)
        got_ok = res.ok
        status = "ok " if got_ok == expect_ok else "FAIL"
        if got_ok != expect_ok:
            failures += 1
        detail = "" if got_ok else f"  [{res.message}]"
        print(f"  {status}  {name}: expected ok={expect_ok}, got ok={got_ok}{detail}")
    print()
    if failures:
        print(f"{failures} case(s) FAILED")
        return 1
    print(f"all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
