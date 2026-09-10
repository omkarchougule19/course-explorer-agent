"""
test_prereqs.py

Checks for app.prereqs.parse_prerequisites - the best-effort parser that
turns a catalog description into AND-ed groups of OR-ed course options.

    .venv/Scripts/python -m evals.test_prereqs

Strings are real, pulled from data/courses.db. The parser is conservative by
design: these assert the shape it should get right, not that it handles
every registrar sentence perfectly.
"""

from app.prereqs import parse_prerequisites

# Each case: (name, description, expected AND-groups as list of sorted
# option-lists, expected clause-level condition substrings)
CASES = [
    ("simple AND of two",
     "Data abstractions. Prerequisite: One of CS 173, MATH 213; CS 225. Credit is not given for both.",
     [[("CS", "173"), ("MATH", "213")], [("CS", "225")]], []),
    ("bare single course",
     "Intro stats. Prerequisite: MATH 112.",
     [[("MATH", "112")]], []),
    ("and-separated pair",
     "Prerequisite: ABE 227 and ABE 228.",
     [[("ABE", "227")], [("ABE", "228")]], []),
    ("one of, three ways",
     "Prerequisite: One of MATH 220, MATH 221, MATH 234.",
     [[("MATH", "220"), ("MATH", "221"), ("MATH", "234")]], []),
    ("pure condition, no courses",
     "Prerequisite: Consent of instructor.",
     [], ["Consent of instructor"]),
    ("course plus trailing condition clause",
     "Prerequisite: STAT 410 or equivalent; knowledge of a programming language.",
     [[("STAT", "410")]], ["knowledge of a programming language"]),
    ("no prerequisite label at all",
     "A survey course. Open to all students.",
     [], []),
    ("concurrent registration group",
     "Prerequisite: MATH 220 or MATH 221; credit or concurrent registration in one of MATH 225, MATH 257, MATH 415.",
     [[("MATH", "220"), ("MATH", "221")],
      [("MATH", "225"), ("MATH", "257"), ("MATH", "415")]], []),
    ("multiple OR groups AND-ed",
     "Prerequisite: ACE 240 or FIN 232; ACE 349 or FIN 230; ACE 449.",
     [[("ACE", "240"), ("FIN", "232")],
      [("ACE", "349"), ("FIN", "230")],
      [("ACE", "449")]], []),
]


def _norm_groups(parsed):
    return [sorted(g.options) for g in parsed.groups]


def main() -> int:
    failures = 0
    for name, desc, want_groups, want_conds in CASES:
        p = parse_prerequisites(desc)
        got_groups = _norm_groups(p)
        want_sorted = [sorted(g) for g in want_groups]
        ok_groups = got_groups == want_sorted
        all_conds = " | ".join(p.conditions + [c for g in p.groups for c in g.conditions]).lower()
        ok_conds = all(c.lower() in all_conds for c in want_conds)
        status = "ok  " if (ok_groups and ok_conds) else "FAIL"
        if not (ok_groups and ok_conds):
            failures += 1
        print(f"  {status} {name}")
        if not ok_groups:
            print(f"        groups: got {got_groups}  want {want_sorted}")
        if not ok_conds:
            print(f"        conditions: got {p.conditions + [c for g in p.groups for c in g.conditions]}  want {want_conds}")

    # Concurrent relation is tagged.
    p = parse_prerequisites(
        "Prerequisite: MATH 220; credit or concurrent registration in MATH 225.")
    rels = {g.relation for g in p.groups}
    if "concurrent" not in rels:
        failures += 1
        print(f"  FAIL concurrent relation not tagged: {rels}")
    else:
        print("  ok   concurrent relation tagged")

    # raw is always retained when a clause exists.
    if parse_prerequisites("Prerequisite: CS 225.").raw != "Prerequisite: CS 225.":
        failures += 1
        print("  FAIL raw not retained")
    else:
        print("  ok   raw sentence retained")

    print()
    print(f"{len(CASES) + 2 - failures}/{len(CASES) + 2} passed"
          if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
