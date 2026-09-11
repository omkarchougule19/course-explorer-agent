"""
test_calendar.py

Checks for app.load_calendar's date parsing, categorisation, and the
end-to-end HTML -> rows extraction, against a committed sample of the real
UIUC Fall 2026 registrar page (evals/fixtures/calendar_fall2026.html).

    .venv/Scripts/python -m evals.test_calendar
"""

from pathlib import Path

from app.load_calendar import _parse_date, _categorize, extract_events

FIXTURE = Path(__file__).parent / "fixtures" / "calendar_fall2026.html"

_DATE_CASES = [
    ("August 24", ("2026-08-24", None)),
    ("Aug 16-23", ("2026-08-16", "2026-08-23")),
    ("Nov 21-29", ("2026-11-21", "2026-11-29")),
    ("December 22", ("2026-12-22", None)),
    ("January 8", ("2027-01-08", None)),        # rolls into next calendar year
    ("Instruction begins", None),               # not a date
    ("Dec 11-17", ("2026-12-11", "2026-12-17")),
]

_CATEGORY_CASES = [
    ("Instruction begins (POT 1 & A courses)", "instruction"),
    ("Last instruction day", "instruction"),
    ("Reading day", "instruction"),
    ('Add deadline and "10th day" drop deadline for full semester', "add"),
    ("Drop deadline without W grade; withdrawal deadline", "drop"),
    ("Fall break", "break"),
    ("Labor Day (no classes)", "holiday"),
    ("Final examination period", "finals"),
    ("Faculty grade entry deadline (2:00 PM)", "grades"),
    ("Spring priority registration opens", "registration"),
    ("Degree conferral", "commencement"),
    ("Housing move-in period", "other"),
]


def main() -> int:
    failures = 0

    for raw, want in _DATE_CASES:
        got = _parse_date(raw, 2026, "fall")
        got_pair = None if got is None else (got[0], got[1])
        if got_pair != want:
            failures += 1
            print(f"  FAIL date {raw!r}: got {got_pair}  want {want}")
        else:
            print(f"  ok   date {raw!r} -> {got_pair}")

    for title, want in _CATEGORY_CASES:
        got = _categorize(title)
        if got != want:
            failures += 1
            print(f"  FAIL cat {title!r}: got {got!r}  want {want!r}")
        else:
            print(f"  ok   cat {title[:45]!r} -> {got}")

    rows = extract_events(FIXTURE.read_text(encoding="utf-8"), 2026, "fall", None)
    by_title = {r[4]: r for r in rows}
    print(f"\n  extracted {len(rows)} events from the fixture")

    checks = [
        ("Instruction begins" in " | ".join(by_title), "instruction-begins event present"),
        (len(rows) >= 12, "at least 12 events"),
        (any(r[3] and r[2] == "2026-11-21" for r in rows), "Fall break carries an end date"),
        (any(r[5] == "finals" and r[2] == "2026-12-11" for r in rows), "finals period categorised + dated"),
        (any(r[5] == "drop" for r in rows), "a drop-deadline event exists"),
        (all(r[0] == 2026 and r[1] == "fall" for r in rows), "every row tagged 2026/fall"),
    ]
    for ok, label in checks:
        if not ok:
            failures += 1
            print(f"  FAIL {label}")
        else:
            print(f"  ok   {label}")

    print()
    print("all checks passed" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
