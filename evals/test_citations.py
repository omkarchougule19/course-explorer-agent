"""
test_citations.py

Checks for app.citations - the deterministic "Sources: …" answer footer.

    .venv/Scripts/python -m evals.test_citations

Offline: no LLM. `sections_freshness` touches the DB but the footer shape is
asserted independently of the exact sync dates.
"""

import os

from app import citations

CAP_SQL = [
    ("SELECT COUNT(*) FROM sections WHERE subject='CS'", {"sections"}),
    ("SELECT s.instructor FROM sections s JOIN meetings m ON s.crn=m.crn", {"sections", "meetings"}),
    ("WITH g AS (SELECT * FROM grade_distributions) SELECT * FROM g", {"grade_distributions"}),
    ("SELECT * FROM gen_ed_categories WHERE subject IN ('CS','STAT')", {"gen_ed_categories"}),
    ("SELECT subject FROM prerequisites WHERE req_subject='CS'", {"prerequisites"}),
    ("SELECT title FROM academic_calendar WHERE category='drop'", {"academic_calendar"}),
    ("not even sql", set()),
]


def main() -> int:
    failures = 0
    os.environ["ANSWER_CITATIONS"] = "1"

    for sql, want in CAP_SQL:
        got = citations.tables_in([sql])
        if got != want:
            failures += 1
            print(f"  FAIL tables_in({sql[:40]!r}): {got}  want {want}")
        else:
            print(f"  ok   tables_in -> {got or '{}'}")

    # empty input -> empty footer
    if citations.sources_footer([], rag_used=False) != "":
        failures += 1
        print("  FAIL empty input should give empty footer")
    else:
        print("  ok   empty input -> ''")

    # a real 2-source query -> a well-formed line
    f = citations.sources_footer(
        ["SELECT * FROM sections s JOIN grade_distributions g "
         "ON s.subject=g.subject WHERE s.subject='CS'"],
        rag_used=False,
    )
    ok = f.startswith("\n\n---\n*Sources: ") and f.endswith(".*") \
        and "UIUC Course Explorer" in f and "wadefagen" in f
    if not ok:
        failures += 1
        print(f"  FAIL 2-source footer malformed: {f!r}")
    else:
        print(f"  ok   2-source footer: {f.strip()!r}")

    # RAG-only answer still gets a source
    f = citations.sources_footer([], rag_used=True)
    if "catalog descriptions" not in f:
        failures += 1
        print(f"  FAIL rag-only footer: {f!r}")
    else:
        print("  ok   rag-only footer names the descriptions source")

    # off switch
    os.environ["ANSWER_CITATIONS"] = "0"
    if citations.sources_footer(["SELECT * FROM sections"], rag_used=True) != "":
        failures += 1
        print("  FAIL ANSWER_CITATIONS=0 did not suppress the footer")
    else:
        print("  ok   ANSWER_CITATIONS=0 suppresses the footer")
    os.environ["ANSWER_CITATIONS"] = "1"

    print()
    print("all checks passed" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
