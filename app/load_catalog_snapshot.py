"""
load_catalog_snapshot.py

One-time historical backfill: downloads a single past term's pre-scraped,
fully-flattened course catalog CSV from wadefagen/datasets and upserts it into
the same `sections` + `meetings` tables scraper.py writes to - so backfilled
and live-scraped rows are indistinguishable to the rest of the app.

This exists so we don't have to re-scrape UIUC's API (with its WAF/rate-limit
risk) for years of history that's already sitting in a free, public CSV.
scraper.py stays responsible for the live/current term(s) going forward; this
script is for past terms only, run once per term you want to backfill.

Free, public CSV, no auth. https://github.com/wadefagen/datasets/tree/main/course-catalog

Usage:
    python -m app.load_catalog_snapshot --term 2026-sp
"""

import argparse
import csv
import io
import re
import sys

import requests

from app.scraper import Meeting, Section, init_db, save_sections

CSV_URL_TEMPLATE = "https://raw.githubusercontent.com/wadefagen/datasets/main/course-catalog/data/{term}.csv"

TERM_ARG_RE = re.compile(r"^(\d{4})-(fa|sp|su|wi)$")
TERM_NAME = {"fa": "fall", "sp": "spring", "su": "summer", "wi": "winter"}


def _first_instructor(raw: str):
    """The CSV joins co-instructors with ';'. scraper.py's live path only ever
    captures one instructor per section/meeting too, so take the first name here
    to keep backfilled rows consistent with what a live scrape would have stored."""
    raw = (raw or "").strip()
    if not raw:
        return None
    return raw.split(";")[0].strip() or None


def fetch_sections(term: str) -> list[Section]:
    url = CSV_URL_TEMPLATE.format(term=term)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))

    sections: dict[tuple, Section] = {}
    seen_meetings: dict[tuple, set] = {}

    for row in reader:
        year_raw = (row.get("Year") or "").strip()
        if not year_raw.isdigit():
            continue
        year = int(year_raw)
        semester = (row.get("Term") or "").strip().lower()
        subject = (row.get("Subject") or "").strip().upper()
        course_number = (row.get("Number") or "").strip()
        crn = (row.get("CRN") or "").strip()
        if not (subject and course_number and crn):
            continue

        key = (year, semester, subject, course_number, crn)
        section = sections.get(key)
        if section is None:
            section = Section(
                year=year, semester=semester, subject=subject, course_number=course_number,
                course_label=(row.get("Name") or "").strip(),
                crn=crn,
                section_name=(row.get("Section") or "").strip() or None,
                instructor=_first_instructor(row.get("Instructors")),
                enrollment_status=(row.get("Enrollment Status") or "").strip() or None,
                credit_hours=(row.get("Section Credit Hours") or "").strip()
                or (row.get("Credit Hours") or "").strip() or None,
                description=(row.get("Description") or "").strip() or None,
                part_of_term=(row.get("Part of Term") or "").strip() or None,
            )
            sections[key] = section
            seen_meetings[key] = set()

        meeting_type = (row.get("Type") or "").strip()
        start_time = (row.get("Start Time") or "").strip()
        if meeting_type or start_time:
            meeting_key = (meeting_type, (row.get("Days of Week") or "").strip(), start_time,
                           (row.get("End Time") or "").strip(), (row.get("Building") or "").strip(),
                           (row.get("Room") or "").strip())
            if meeting_key not in seen_meetings[key]:
                seen_meetings[key].add(meeting_key)
                section.meetings.append(
                    Meeting(
                        meeting_type=meeting_type or None,
                        days_of_week=(row.get("Days of Week") or "").strip() or None,
                        start_time=start_time or None,
                        end_time=(row.get("End Time") or "").strip() or None,
                        building=(row.get("Building") or "").strip() or None,
                        room=(row.get("Room") or "").strip() or None,
                        instructor=_first_instructor(row.get("Instructors")),
                    )
                )

    return list(sections.values())


def run(term: str) -> int:
    conn = init_db()
    try:
        sections = fetch_sections(term)
        if not sections:
            print(f"No rows found for term '{term}' - check the term format (e.g. 2026-sp) "
                  f"and that {CSV_URL_TEMPLATE.format(term=term)} exists.", flush=True)
            return 0
        n = save_sections(conn, sections)
        return n
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill one past term from wadefagen/datasets into sections/meetings")
    parser.add_argument("--term", required=True, help="Format YYYY-xx, e.g. 2026-sp, 2025-fa, 2025-su, 2025-wi")
    args = parser.parse_args()

    if not TERM_ARG_RE.match(args.term):
        parser.error("--term must look like 2026-sp (year-fa/sp/su/wi)")

    print(f"Downloading catalog snapshot for {args.term} ...", flush=True)
    try:
        n = run(args.term)
    except requests.RequestException as exc:
        print(f"Download failed: {exc}", flush=True)
        sys.exit(1)
    print(f"Backfilled {n} section row(s) for {args.term}.", flush=True)
