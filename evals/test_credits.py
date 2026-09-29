"""
Offline checks for app/credits.py (credit hours, graduate credit and
restriction parsing). No database, no network.

    python -m evals.test_credits
"""
from app.credits import credit_fields, parse_graduate, parse_hours, parse_restriction

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


check("single value", parse_hours("3 hours.") == (3.0, 3.0))
check("OR range", parse_hours("3 OR 4 hours.") == (3.0, 4.0))
check("TO range", parse_hours("1 TO 4 hours.") == (1.0, 4.0))
check("half hours", parse_hours("0.5 hours.") == (0.5, 0.5))
check("singular 'hour'", parse_hours("1 hour.") == (1.0, 1.0))
check("unknown shape -> None", parse_hours("Variable") == (None, None) and parse_hours(None) == (None, None))

CS440 = ("Major topics in AI. Same as ECE 448. 3 undergraduate hours. 3 or 4 graduate hours. "
         "Prerequisite: CS 225.")
check("400-level with graduate hours", parse_graduate("440", CS440, 3, 4) == ("yes", 3.0, 4.0))
check("'undergraduate hours' is not graduate credit",
      parse_graduate("499", "3 undergraduate hours. No graduate credit.", 3, 3) == ("no", None, None))
check("graduate-only course", parse_graduate("400", "No undergraduate credit. 3 graduate hours.", 3, 3)
      == ("yes", 3.0, 3.0))
check("4 graduate hours only", parse_graduate("444", "3 undergraduate hours. 4 graduate hours.", 3, 4)
      == ("yes", 4.0, 4.0))
check("singular 'graduate hour'", parse_graduate("401", "1 undergraduate hour. 1 graduate hour.", 1, 1)
      == ("yes", 1.0, 1.0))
check("a repeat limit is not the course's credit",
      parse_graduate("490", "May be repeated to a maximum of 6 undergraduate hours or 8 graduate hours.", 1, 4)
      == (None, None, None))
check("400-level with no statement is unknown", parse_graduate("450", "Numerical methods.", 3, 4)
      == (None, None, None))
check("500-level defaults to graduate, with credit_hours", parse_graduate("598", "Topics.", 2, 4)
      == ("yes", 2.0, 4.0))
check("300-level defaults to no graduate credit", parse_graduate("374", "Algorithms.", 4, 4)
      == ("no", None, None))
check("explicit statement beats the level default",
      parse_graduate("500", "No graduate credit.", 4, 4) == ("no", None, None))

check("restriction sentence kept verbatim",
      parse_restriction("Acting. Restricted to Theatre majors and minors. Prerequisite: THEA 101.")
      == "Restricted to Theatre majors and minors.")
check("'majors only' counts", parse_restriction("Prerequisite: For majors only; junior standing.")
      == "Prerequisite: For majors only; junior standing.")
check("no restriction -> None", parse_restriction("Covers data structures.") is None)
check("long restrictions are capped", len(parse_restriction("Restricted to " + "x " * 400 + ".")) <= 303)

check("credit_fields in column order",
      credit_fields("440", "3 OR 4 hours.", CS440) == (3.0, 4.0, "yes", 3.0, 4.0, None))

print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
