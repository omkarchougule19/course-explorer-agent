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
    ("First day of instruction for Spring POT 1 (full semester) courses", "instruction"),
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

    # Which calendar year an undated month belongs to, per season: a spring
    # calendar opens with last autumn's registration, a fall one ends with
    # January deadlines.
    for raw, year, sem, want in [
        ("November 2", 2027, "spring", ("2026-11-02", None)),
        ("October 5", 2027, "spring", ("2026-10-05", None)),
        ("January 19", 2027, "spring", ("2027-01-19", None)),
        ("June 22", 2027, "spring", ("2027-06-22", None)),
        ("January 8", 2026, "fall", ("2027-01-08", None)),
        ("March 9", 2026, "fall", ("2026-03-09", None)),
        ("March 8", 2027, "summer", ("2027-03-08", None)),
    ]:
        got = _parse_date(raw, year, sem)
        got_pair = None if got is None else (got[0], got[1])
        if got_pair != want:
            failures += 1
            print(f"  FAIL year {raw!r} on {sem} {year}: got {got_pair}  want {want}")
        else:
            print(f"  ok   year {raw!r} on {sem} {year} -> {got_pair[0]}")

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
    # The live registrar pages' heading shapes (2026-11): a date or range
    # followed by a label starts a new date. Both used to be filed under the
    # previous heading's date.
    live = extract_events(
        "<h2>December 10</h2><ul><li>Reading day (no classes, no final examinations)</li></ul>"
        "<h2>December 11–17 Weekdays only</h2><ul><li>Final examination period</li></ul>"
        "<h2>September 4 – “10th day” add/drop deadline</h2><ul><li>Deadline for ALL to drop</li></ul>"
        "<h2>March 13 – 21</h2><ul><li>Spring Break</li></ul>"
        "<h2>October 5</h2><ul><li>Spring Class Schedule is available on Course Explorer</li></ul>"
        "<h2>October 13 at 2:00 PM</h2><ul><li>Faculty deadline for reporting midterm grades</li></ul>",
        2026, "fall", None)
    when = {r[4]: (r[2], r[3]) for r in live}
    checks += [
        (when.get("Final examination period (weekdays only)") == ("2026-12-11", "2026-12-17")
         and "Weekdays only" not in when,
         "finals under 'December 11–17 Weekdays only' get Dec 11-17 and the qualifier, "
         "not the reading day or a stray 'Weekdays only' event"),
        (when.get("Reading day (no classes, no final examinations)", ("",))[0] == "2026-12-10",
         "the reading day keeps its own date"),
        (when.get("“10th day” add/drop deadline", ("",))[0] == "2026-09-04"
         and when.get("Deadline for ALL to drop", ("",))[0] == "2026-09-04",
         "a 'date – label' heading is an event and dates what follows"),
        (when.get("Spring Break") == ("2026-03-13", "2026-03-21"),
         "a bare range heading still reads as a range"),
        (when.get("Faculty deadline for reporting midterm grades (2:00 PM)", ("",))[0] == "2026-10-13",
         "an 'October 13 at 2:00 PM' heading dates the event after it, with the time"),
        (when.get("Spring Class Schedule is available on Course Explorer", ("",))[0] == "2026-10-05"
         and not any("at 2:00" in t for t in when),
         "the event before a timed heading keeps its date; no stray time-only event"),
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
