"""
test_schedule_filters.py

Offline checks (no LLM, no network) for the /sections schedule filters behind
the Browse panel's "vibe" chips (no 8 AMs, done by 5, no Fridays):

    .venv/Scripts/python -m evals.test_schedule_filters
"""

import os

# Score against the local SQLite snapshot, never live Neon.
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_RO"] = ""

from datetime import time

from fastapi.testclient import TestClient

from app import api

failures = []


def check(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def m(days, start, end):
    return {"days_of_week": days, "start_time": start, "end_time": end}


nine, five = time(9, 0), time(17, 0)

# Both stored spellings of a time parse the same; junk parses to None
check("'03:00 PM' (SQLite) parses", api._parse_time("03:00 PM") == time(15, 0))
check("'03:00PM' (Neon) parses", api._parse_time("03:00PM") == time(15, 0))
check("'08:00AM' parses", api._parse_time("08:00AM") == time(8, 0))
check("'ARRANGED' -> None", api._parse_time("ARRANGED") is None)

# _meetings_fit: the pure rule behind every chip
check("8 AM lecture fails 'no 8 AMs'", not api._meetings_fit([m("MWF", "08:00 AM", "08:50 AM")], nine, None, set()))
check("9 AM lecture passes 'no 8 AMs'", api._meetings_fit([m("MWF", "09:00 AM", "09:50 AM")], nine, None, set()))
check("Neon-style '08:00AM' also fails 'no 8 AMs'", not api._meetings_fit([m("MWF", "08:00AM", "08:50AM")], nine, None, set()))
check("one early meeting (the discussion) fails the whole section",
      not api._meetings_fit([m("TR", "11:00 AM", "12:15 PM"), m("F", "08:00 AM", "08:50 AM")], nine, None, set()))
check("4-6 PM class fails 'done by 5'", not api._meetings_fit([m("T", "04:00 PM", "06:00 PM")], None, five, set()))
check("ends exactly at 5 PM passes 'done by 5'", api._meetings_fit([m("T", "04:00 PM", "05:00 PM")], None, five, set()))
check("Friday meeting fails 'no Fridays'", not api._meetings_fit([m("MWF", "10:00 AM", "10:50 AM")], None, None, {"F"}))
check("MW passes 'no Fridays'", api._meetings_fit([m("MW", "10:00 AM", "10:50 AM")], None, None, {"F"}))
check("no meetings (online) passes every filter", api._meetings_fit([], nine, five, {"F"}))
check("unparseable time is ignored, not a crash", api._meetings_fit([m("MW", "ARR", "ARR")], nine, five, set()))
check("filters combine (AND)", not api._meetings_fit([m("MW", "10:00 AM", "05:30 PM")], nine, five, set()))

# HTTP behavior that fails before touching the database
client = TestClient(api.app)
r = client.get("/sections?starts_after=09:00")
check("schedule filter without a subject -> 400", r.status_code == 400)
r = client.get("/sections?subject=CS&starts_after=9am")
check("bad time format -> 400", r.status_code == 400)

# End to end on the local snapshot, if it has data
r = client.get("/sections?subject=CS&limit=1000")
if r.status_code == 200 and r.json():
    base = r.json()
    r2 = client.get("/sections?subject=CS&starts_after=09:00&limit=1000")
    check("filtered query returns 200", r2.status_code == 200)
    kept = r2.json()
    check("filter never adds sections", len(kept) <= len(base))
    check("filter kept a subset of the same CRNs", {x["crn"] for x in kept} <= {x["crn"] for x in base})
    early = client.get("/meetings", params={"crns": ",".join(x["crn"] for x in kept[:200]),
                                             "year": kept[0]["year"], "semester": kept[0]["semester"]}) if kept else None
    if early is not None and early.status_code == 200:
        bad = [x for x in early.json()
               if api._parse_time(x["start_time"]) and api._parse_time(x["start_time"]) < nine]
        check("no kept section has a meeting before 9 AM", not bad)
else:
    print("  (no local CS data; skipped the end-to-end check)")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all checks passed")
