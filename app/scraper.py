"""
scraper.py

Pulls course, section, and enrollment data from UIUC's public Course Explorer
API (CISAPI) and loads it into a local SQLite database.

Public, no auth required. Documented at https://courses.illinois.edu/cisdocs/explorer

Two separate modules of the API are used:

Schedule module (per term - what's actually offered, with live enrollment):
    /cisapp/explorer/schedule.xml
    /cisapp/explorer/schedule/{year}.xml
    /cisapp/explorer/schedule/{year}/{semester}.xml
    /cisapp/explorer/schedule/{year}/{semester}/{subject}.xml
    /cisapp/explorer/schedule/{year}/{semester}/{subject}/{course}.xml
    /cisapp/explorer/schedule/{year}/{semester}/{subject}/{course}/{crn}.xml   <- per section detail

Catalog module (per course - static info like the course description,
independent of whether/when it's actually scheduled):
    /cisapp/explorer/catalog/{year}/{semester}/{subject}/{course}.xml         <- course description

The schedule course-level XML only lists section id (CRN) and a short name.
Instructor and live enrollment status live one level deeper, at the CRN
endpoint, and the course description lives in the separate catalog module, so
a full "detailed" scrape costs two extra requests per course (one for the
description, one per section for instructor/enrollment). Use --fast to skip
all of that and store the section id/name list only.

Courses within a subject are fetched concurrently (--concurrency, default
10 workers) since course-level fetching is what dominates a full scrape.
All database writes happen on the main thread as each course finishes, so
there's no SQLite concurrency to worry about.

Usage:
    python scraper.py --year 2026 --semester fall --subjects CS,STAT,IS
    python scraper.py --year 2026 --semester fall --subjects CS --fast
    python scraper.py --year 2026 --semester fall --concurrency 15
    python scraper.py --year 2026 --semester fall --skip-recent 24
"""

import argparse
import threading
import time
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import requests
from dotenv import load_dotenv
from tqdm import tqdm

from app import db
from app import embeddings
from app.db import DB_PATH

# Load DATABASE_URL (and any other vars) from the project-root .env, the same
# way agent.py / api.py do. Without this a plain `python -m app.scraper` never
# sees DATABASE_URL and silently writes to local SQLite instead of Neon.
load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL = "https://courses.illinois.edu/cisapp/explorer/schedule"
CATALOG_BASE_URL = "https://courses.illinois.edu/cisapp/explorer/catalog"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://courses.illinois.edu/schedule/",
}

# Populated once by warmup() before scraping starts. Some WAFs (e.g. Akamai)
# only allow API access to clients that first picked up a session cookie from
# a normal page load; every thread's Session gets these cookies at creation time.
_warmup_cookies: dict = {}
_warmup_lock = threading.Lock()


def warmup(year: int, semester: str) -> bool:
    """Visit the normal HTML schedule page once, like a browser would, before hitting
    the XML API. Picks up any WAF/session cookie that a bare API request wouldn't have.
    Returns True if the warm-up request itself succeeded (not a guarantee the API will)."""
    global _warmup_cookies
    url = f"https://courses.illinois.edu/schedule/{year}/{semester}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        with _warmup_lock:
            _warmup_cookies = resp.cookies.get_dict()
        return resp.ok
    except requests.RequestException as exc:
        tqdm.write(f"  [warn] warm-up request to {url} failed: {exc}")
        return False

# One requests.Session per worker thread (Sessions aren't safe to share across
# threads, but a thread-local pool gives every worker its own reused,
# keep-alive connection instead of opening a fresh TCP/TLS connection per request).
_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update(HEADERS)
        if _warmup_cookies:
            session.cookies.update(_warmup_cookies)
        _thread_local.session = session
    return _thread_local.session


@dataclass
class Meeting:
    meeting_type: Optional[str]
    days_of_week: Optional[str]
    start_time: Optional[str]
    end_time: Optional[str]
    building: Optional[str]
    room: Optional[str]
    instructor: Optional[str] = None


@dataclass
class Section:
    year: int
    semester: str
    subject: str
    course_number: str
    course_label: str
    crn: str
    section_name: Optional[str]
    instructor: Optional[str]
    enrollment_status: Optional[str]
    credit_hours: Optional[str]
    description: Optional[str] = None
    part_of_term: Optional[str] = None
    section_start_date: Optional[str] = None
    section_end_date: Optional[str] = None
    meetings: list = None

    def __post_init__(self):
        if self.meetings is None:
            self.meetings = []


def fetch_xml(url: str, retries: int = 3, backoff: float = 1.5) -> Optional[ET.Element]:
    """GET a Course Explorer URL and return the parsed XML root, with basic retry/backoff.

    404s mean "this doesn't exist" (e.g. a subject with no courses this term) and are not
    retried. 429s back off longer since they mean we're being rate limited. Everything else
    (timeouts, connection resets, 5xx, malformed XML) gets the normal retry/backoff treatment.
    Safe to call from multiple threads: each thread gets its own Session.
    """
    session = get_session()
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 404:
                return None
            if resp.status_code == 403:
                # Not worth retrying - a 403 is a deliberate rejection, not a transient
                # hiccup, so hitting it again with the same session won't help. Surfaced
                # (unlike 404) since it usually means something's actively blocking us.
                tqdm.write(f"  [warn] 403 Forbidden on {url}")
                return None
            if resp.status_code == 429:
                wait = backoff * (attempt + 2) * 2  # back off harder on rate limiting
                tqdm.write(f"  [warn] rate limited on {url}, waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as exc:
            if attempt == retries - 1:
                tqdm.write(f"  [warn] giving up on {url}: {exc}")
                return None
            time.sleep(backoff * (attempt + 1))
    return None


def strip_ns(tag: str) -> str:
    """Course Explorer namespaces only the root element (e.g. {http://rest.cis.illinois.edu}subject).
    Children are unnamespaced, but this helper makes tag comparisons safe either way."""
    return tag.split("}")[-1] if "}" in tag else tag


def list_subjects(year: int, semester: str) -> list[str]:
    """Return every subject code offered in a given term (e.g. CS, STAT, IS)."""
    url = f"{BASE_URL}/{year}/{semester}.xml"
    root = fetch_xml(url)
    if root is None:
        return []
    # subjects are nested under a <subjects> wrapper; .iter() finds them regardless of depth
    return [el.get("id") for el in root.iter() if strip_ns(el.tag) == "subject" and el.get("id")]


def list_courses(year: int, semester: str, subject: str) -> list[str]:
    """Return every course number offered under a subject in a given term."""
    url = f"{BASE_URL}/{year}/{semester}/{subject}.xml"
    root = fetch_xml(url)
    if root is None:
        return []
    return [el.get("id") for el in root.iter() if strip_ns(el.tag) == "course" and el.get("id")]


# Candidate tag names for meeting-block fields. As with DESCRIPTION_TAGS below, the
# exact schema couldn't be verified against the live API from this environment (it's
# behind the same WAF the scraper works around), so this checks plausible field names
# per the public API wrapper docs rather than assuming one - missing/renamed fields
# just come back as None instead of crashing the scrape.
MEETING_TAGS = ("meeting",)
MEETING_TYPE_TAGS = ("type",)
MEETING_START_TAGS = ("start",)
MEETING_END_TAGS = ("end",)
MEETING_DAYS_TAGS = ("daysOfTheWeek",)
MEETING_ROOM_TAGS = ("roomNumber",)
MEETING_BUILDING_TAGS = ("buildingName",)
PART_OF_TERM_TAGS = ("partOfTerm",)
SECTION_START_DATE_TAGS = ("startDate",)
SECTION_END_DATE_TAGS = ("endDate",)


def _child_text(el: ET.Element, tags: tuple) -> Optional[str]:
    """First direct child of el whose (namespace-stripped) tag is in tags, text stripped."""
    for child in el:
        if strip_ns(child.tag) in tags and child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_meeting(meeting_el: ET.Element) -> Meeting:
    instructor = None
    for el in meeting_el.iter():
        if strip_ns(el.tag) == "instructor" and el.text and el.text.strip():
            instructor = el.text.strip()
            break
    return Meeting(
        meeting_type=_child_text(meeting_el, MEETING_TYPE_TAGS),
        days_of_week=_child_text(meeting_el, MEETING_DAYS_TAGS),
        start_time=_child_text(meeting_el, MEETING_START_TAGS),
        end_time=_child_text(meeting_el, MEETING_END_TAGS),
        building=_child_text(meeting_el, MEETING_BUILDING_TAGS),
        room=_child_text(meeting_el, MEETING_ROOM_TAGS),
        instructor=instructor,
    )


def fetch_section_detail(year: int, semester: str, subject: str, course_number: str, crn: str) -> dict:
    """Fetch the per section XML for instructor, enrollment status, meeting times/locations,
    and part-of-term/date info. One request per CRN."""
    url = f"{BASE_URL}/{year}/{semester}/{subject}/{course_number}/{crn}.xml"
    root = fetch_xml(url)
    if root is None:
        return {}

    instructor = None
    for el in root.iter():
        if strip_ns(el.tag) == "instructor" and el.text and el.text.strip():
            instructor = el.text.strip()
            break

    # enrollmentStatus (descriptive, e.g. "Open"/"Closed") and sectionStatusCode
    # (a short code, e.g. "A") are distinct fields in UIUC's schema, not
    # interchangeable - confirmed against a working reference scraper hitting
    # this same API. Scanning for both in one pass and taking whichever tag
    # happens to appear first in document order previously meant the stored
    # value silently flipped between a descriptive status and a raw code
    # depending on a course's internal XML structure. Two separate passes,
    # preferring the descriptive field, fixes that.
    enrollment_status = None
    for el in root.iter():
        if strip_ns(el.tag) == "enrollmentStatus" and el.text and el.text.strip():
            enrollment_status = el.text.strip()
            break
    if enrollment_status is None:
        for el in root.iter():
            if strip_ns(el.tag) == "sectionStatusCode" and el.text and el.text.strip():
                enrollment_status = el.text.strip()
                break

    meetings = [_parse_meeting(el) for el in root.iter() if strip_ns(el.tag) in MEETING_TAGS]

    part_of_term = None
    start_date = None
    end_date = None
    for el in root.iter():
        tag = strip_ns(el.tag)
        if part_of_term is None and tag in PART_OF_TERM_TAGS and el.text and el.text.strip():
            part_of_term = el.text.strip()
        if start_date is None and tag in SECTION_START_DATE_TAGS and el.text and el.text.strip():
            start_date = el.text.strip()
        if end_date is None and tag in SECTION_END_DATE_TAGS and el.text and el.text.strip():
            end_date = el.text.strip()

    return {
        "instructor": instructor,
        "enrollment_status": enrollment_status,
        "meetings": meetings,
        "part_of_term": part_of_term,
        "start_date": start_date,
        "end_date": end_date,
    }


# Candidate tag names for the course description in the catalog XML. The exact
# schema (cisapi.xsd) couldn't be verified against the live API from this
# environment, so this checks a few plausible names rather than assuming one -
# if the real tag isn't among these, fetch_course_description() will just
# return None (graceful no-op) instead of crashing.
DESCRIPTION_TAGS = ("description", "courseDescription", "descr")


def fetch_course_description(year: int, semester: str, subject: str, course_number: str) -> Optional[str]:
    """Fetch a course's catalog description. One request per course, independent of
    section/term scheduling data. Returns None if there's no description, the course
    isn't in the catalog module, or the request fails - never raises."""
    url = f"{CATALOG_BASE_URL}/{year}/{semester}/{subject}/{course_number}.xml"
    root = fetch_xml(url)
    if root is None:
        return None
    for el in root.iter():
        if strip_ns(el.tag) in DESCRIPTION_TAGS:
            # itertext() rather than .text, in case the description has inline
            # markup (e.g. a nested link) rather than being plain text.
            text = "".join(el.itertext()).strip()
            if text:
                return text
    return None


def fetch_course_sections(
    year: int, semester: str, subject: str, course_number: str, detailed: bool = True, section_delay: float = 0.1
) -> list[Section]:
    """Fetch all sections for a single course. In detailed mode, also fetch instructor
    and enrollment status per section (one extra request per CRN), plus the course
    description from the catalog module (one extra request per course, not per section).

    Pure I/O + parsing, no database access, so it's safe to run inside a worker thread.
    """
    url = f"{BASE_URL}/{year}/{semester}/{subject}/{course_number}.xml"
    root = fetch_xml(url)
    if root is None:
        return []

    course_label = ""
    credit_hours = None
    for el in root:
        tag = strip_ns(el.tag)
        if tag == "label" and not course_label:
            course_label = (el.text or "").strip()
        elif tag == "creditHours":
            credit_hours = (el.text or "").strip() or None

    description = None
    if detailed:
        description = fetch_course_description(year, semester, subject, course_number)

    sections: list[Section] = []
    for el in root.iter():
        if strip_ns(el.tag) != "section" or not el.get("id"):
            continue
        crn = el.get("id")
        name_el = None
        for child in el:
            if strip_ns(child.tag) == "name":
                name_el = child
                break
        section_name = (name_el.text or "").strip() if name_el is not None else None

        instructor = None
        enrollment_status = None
        part_of_term = None
        section_start_date = None
        section_end_date = None
        meetings: list[Meeting] = []
        if detailed:
            detail = fetch_section_detail(year, semester, subject, course_number, crn)
            instructor = detail.get("instructor")
            enrollment_status = detail.get("enrollment_status")
            part_of_term = detail.get("part_of_term")
            section_start_date = detail.get("start_date")
            section_end_date = detail.get("end_date")
            meetings = detail.get("meetings") or []
            time.sleep(section_delay)

        sections.append(
            Section(
                year=year, semester=semester, subject=subject, course_number=course_number,
                course_label=course_label, crn=crn, section_name=section_name,
                instructor=instructor, enrollment_status=enrollment_status, credit_hours=credit_hours,
                description=description, part_of_term=part_of_term,
                section_start_date=section_start_date, section_end_date=section_end_date,
                meetings=meetings,
            )
        )
    return sections


def init_db(db_path: Optional[Path] = None) -> db.Connection:
    try:
        conn = db.get_connection(db_path)
    except Exception as exc:
        raise SystemExit(f"Can't open database: {exc}")

    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS sections (
                id {db.autoincrement_pk()},
                year INTEGER NOT NULL,
                semester TEXT NOT NULL,
                subject TEXT NOT NULL,
                course_number TEXT NOT NULL,
                course_label TEXT,
                crn TEXT NOT NULL,
                section_name TEXT,
                instructor TEXT,
                enrollment_status TEXT,
                credit_hours TEXT,
                description TEXT,
                scraped_at {db.current_timestamp_default()},
                UNIQUE(year, semester, subject, course_number, crn)
            )
            """
        )
        # Migration for databases created before these columns existed -
        # ALTER TABLE ADD COLUMN instead of requiring a fresh scrape from scratch.
        existing_cols = db.existing_columns(conn, "sections")
        for col in ("description", "part_of_term", "section_start_date", "section_end_date"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE sections ADD COLUMN {col} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subject ON sections(subject)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_term ON sections(year, semester)")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS meetings (
                id {db.autoincrement_pk()},
                year INTEGER NOT NULL,
                semester TEXT NOT NULL,
                subject TEXT NOT NULL,
                course_number TEXT NOT NULL,
                crn TEXT NOT NULL,
                meeting_type TEXT,
                days_of_week TEXT,
                start_time TEXT,
                end_time TEXT,
                building TEXT,
                room TEXT,
                instructor TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meetings_section "
            "ON meetings(year, semester, subject, course_number, crn)"
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        raise SystemExit(f"Can't initialize schema: {exc}")
    return conn


SECTION_COLUMNS = (
    "year", "semester", "subject", "course_number", "course_label", "crn",
    "section_name", "instructor", "enrollment_status", "credit_hours", "description",
    "part_of_term", "section_start_date", "section_end_date",
)
SECTION_CONFLICT_COLUMNS = ("year", "semester", "subject", "course_number", "crn")


def save_sections(conn: db.Connection, sections: Iterable[Section]) -> int:
    sections = list(sections)
    rows = [
        (s.year, s.semester, s.subject, s.course_number, s.course_label, s.crn,
         s.section_name, s.instructor, s.enrollment_status, s.credit_hours, s.description,
         s.part_of_term, s.section_start_date, s.section_end_date)
        for s in sections
    ]
    if not rows:
        return 0
    try:
        db.upsert(conn, "sections", SECTION_COLUMNS, rows, SECTION_CONFLICT_COLUMNS)

        # Meetings are a one-to-many child of (year, semester, subject, course_number,
        # crn), and can legitimately change between scrapes (room/time changes), so the
        # simplest correct upsert is delete-then-reinsert per section rather than trying
        # to diff individual meeting rows.
        meeting_rows = []
        for s in sections:
            if not s.meetings:
                continue
            conn.execute(
                "DELETE FROM meetings WHERE year = ? AND semester = ? AND subject = ? "
                "AND course_number = ? AND crn = ?",
                (s.year, s.semester, s.subject, s.course_number, s.crn),
            )
            for m in s.meetings:
                meeting_rows.append(
                    (s.year, s.semester, s.subject, s.course_number, s.crn,
                     m.meeting_type, m.days_of_week, m.start_time, m.end_time,
                     m.building, m.room, m.instructor)
                )
        if meeting_rows:
            conn.executemany(
                """
                INSERT INTO meetings
                (year, semester, subject, course_number, crn,
                 meeting_type, days_of_week, start_time, end_time, building, room, instructor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                meeting_rows,
            )

        conn.commit()
    except Exception as exc:
        tqdm.write(f"  [warn] failed to save {len(rows)} row(s), skipping this batch: {exc}")
        conn.rollback()
        return 0
    return len(rows)


def recently_scraped_courses(conn: db.Connection, year: int, semester: str, subject: str, cutoff) -> set[str]:
    """Course numbers under this subject/term whose sections were ALL last scraped at or
    after `cutoff` (a datetime). Used to skip re-fetching courses that were already
    scraped recently. `cutoff` is formatted as a 'YYYY-MM-DD HH:MM:SS' string for SQLite
    (matching its CURRENT_TIMESTAMP) but passed through as a real datetime for Postgres,
    whose scraped_at column is a proper TIMESTAMP."""
    cutoff_param = cutoff if conn.backend == "postgres" else cutoff.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT course_number FROM sections
        WHERE year = ? AND semester = ? AND subject = ?
        GROUP BY course_number
        HAVING MIN(scraped_at) >= ?
        """,
        (year, semester, subject, cutoff_param),
    ).fetchall()
    return {r["course_number"] for r in rows}


def run(
    year: int,
    semester: str,
    subjects: Optional[list[str]] = None,
    fast: bool = False,
    concurrency: int = 10,
    skip_recent_hours: Optional[float] = None,
    section_delay: float = 0.1,
):
    conn = init_db()
    embeddings.init_course_embeddings_table(conn)  # no-op on SQLite, see embeddings.py

    print("Warming up session (visiting the schedule page like a browser first)...", flush=True)
    warmup(year, semester)

    probe_url = f"{BASE_URL}/{year}/{semester}.xml"
    if fetch_xml(probe_url) is None:
        conn.close()
        print(
            f"\nCan't reach the Course Explorer API ({probe_url}) even after warming up "
            f"with a normal page load first. This isn't a bug in the scraper logic - the "
            f"request is being rejected before it even gets to fetching course data. "
            f"Worth checking:\n"
            f"  - Open that exact URL directly in your own browser. If it 403s there too, "
            f"the API is currently blocking automated access outright (possibly heavier "
            f"than usual during fall registration), and no amount of header tweaking here "
            f"will fix it - we'd need to fall back to scraping the HTML schedule pages instead.\n"
            f"  - Try again in a while, this kind of blocking is often temporary.\n"
            f"  - Try a different network (e.g. mobile hotspot) to rule out an IP-based block.\n",
            flush=True,
        )
        return

    target_subjects = subjects or list_subjects(year, semester)

    if not target_subjects:
        conn.close()
        print(
            f"No subjects found for {semester} {year}. Double check the year/semester "
            f"(e.g. --year 2026 --semester fall) or --subjects codes, then try again.",
            flush=True,
        )
        return

    mode = "fast" if fast else "detailed"
    print(
        f"Scraping {len(target_subjects)} subject(s) for {semester} {year} "
        f"({mode} mode, {concurrency} concurrent workers)...",
        flush=True,
    )

    cutoff = None
    if skip_recent_hours:
        cutoff = datetime.utcnow() - timedelta(hours=skip_recent_hours)
        print(f"Skipping courses already scraped within the last {skip_recent_hours}h.", flush=True)

    total = 0
    skipped_total = 0
    subject_bar = tqdm(target_subjects, desc="Subjects", unit="subj", position=0)
    try:
        for subject in subject_bar:
            subject_bar.set_postfix_str(f"{subject} | {total} sections saved")
            try:
                courses = list_courses(year, semester, subject)
            except Exception as exc:  # noqa: BLE001 - keep the run alive regardless of cause
                tqdm.write(f"  [warn] couldn't list courses for {subject}, skipping subject: {exc}")
                continue

            if not courses:
                tqdm.write(f"  [warn] no courses found for {subject} in {semester} {year}, skipping")
                continue

            fresh_courses: set[str] = set()
            if cutoff:
                try:
                    fresh_courses = recently_scraped_courses(conn, year, semester, subject, cutoff)
                except Exception as exc:
                    tqdm.write(f"  [warn] couldn't check recent-scrape status for {subject}, fetching all: {exc}")

            to_fetch = [c for c in courses if c not in fresh_courses]
            skipped_here = len(courses) - len(to_fetch)
            skipped_total += skipped_here

            # Per-course bar: this is what was missing before. In detailed mode each
            # course can trigger several section-level HTTP requests, so without this
            # you'd stare at a blank terminal for minutes at a time. Courses within a
            # subject are fetched concurrently; only DB writes happen on the main thread.
            course_bar = tqdm(total=len(courses), desc=f"  {subject}", unit="course", position=1, leave=False)
            course_bar.update(skipped_here)
            subject_sections = 0

            if to_fetch:
                pool = ThreadPoolExecutor(max_workers=concurrency)
                try:
                    futures = {
                        pool.submit(
                            fetch_course_sections, year, semester, subject, course_number, not fast, section_delay
                        ): course_number
                        for course_number in to_fetch
                    }
                    for future in as_completed(futures):
                        course_number = futures[future]
                        try:
                            sections = future.result()
                        except Exception as exc:  # noqa: BLE001 - one bad course shouldn't kill the run
                            tqdm.write(f"  [warn] {subject} {course_number} failed, skipping: {exc}")
                            sections = []
                        if sections:
                            n = save_sections(conn, sections)
                            total += n
                            subject_sections += n
                            # Description is per-course (identical across every section
                            # in this batch), so embed it once per course, not per
                            # section. No-op on SQLite / fast mode (no description) -
                            # see embeddings.py.
                            try:
                                embeddings.save_course_embedding(
                                    conn, subject, course_number, sections[0].description
                                )
                            except Exception as exc:  # noqa: BLE001 - one bad embed shouldn't kill the run
                                tqdm.write(f"  [warn] couldn't embed {subject} {course_number}: {exc}")
                        course_bar.set_postfix_str(f"{subject_sections} sections")
                        course_bar.update(1)
                except KeyboardInterrupt:
                    # Cancel anything not already running instead of waiting for the
                    # whole in-flight batch to drain (default shutdown behavior).
                    pool.shutdown(wait=False, cancel_futures=True)
                    course_bar.close()
                    raise
                else:
                    pool.shutdown(wait=True)
            course_bar.close()

            note = f", {skipped_here} skipped (recent)" if skipped_here else ""
            tqdm.write(f"  [{subject}] {len(courses)} courses{note}, {subject_sections} sections (running total: {total})")

        subject_bar.close()
        target = "Neon Postgres (DATABASE_URL)" if db.is_postgres() else str(DB_PATH)
        summary = f"Done. {total} sections saved to {target}."
        if skipped_total:
            summary += f" {skipped_total} course(s) skipped as recently scraped."
        print(summary, flush=True)
    except KeyboardInterrupt:
        subject_bar.close()
        print(
            f"\nInterrupted. {total} section(s) saved so far are safe in "
            f"{'Neon Postgres' if db.is_postgres() else DB_PATH} "
            f"(each course commits as it finishes). Re-run the same command to pick up "
            f"where you left off, rows are upserted so nothing gets duplicated.",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape UIUC Course Explorer into SQLite")
    parser.add_argument("--year", type=int, required=True, help="e.g. 2026")
    parser.add_argument(
        "--semester", type=str, required=True,
        choices=["fall", "spring", "summer", "winter"],
        help="fall | spring | summer | winter",
    )
    parser.add_argument("--subjects", type=str, default=None, help="Comma separated subject codes, e.g. CS,STAT,IS. Omit to scrape every subject (slow).")
    parser.add_argument("--fast", action="store_true", help="Skip per section detail fetch (no instructor/enrollment, much faster)")
    parser.add_argument("--concurrency", type=int, default=10, help="Number of courses to fetch in parallel (default 10). Lower this if you start seeing rate-limit warnings.")
    parser.add_argument("--skip-recent", type=float, default=None, metavar="HOURS", help="Skip re-fetching courses already scraped within this many hours. Omit to always refetch.")
    parser.add_argument("--section-delay", type=float, default=0.1, help="Seconds to pause between per-section detail requests within a course, in detailed mode (default 0.1)")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    subj_list = None
    if args.subjects:
        subj_list = [s.strip().upper() for s in args.subjects.split(",") if s.strip()]

    try:
        run(
            args.year,
            args.semester.lower(),
            subj_list,
            args.fast,
            args.concurrency,
            args.skip_recent,
            args.section_delay,
        )
    except KeyboardInterrupt:
        # Belt-and-suspenders: run() already handles this, but a Ctrl+C between
        # calls (e.g. during init_db) would otherwise still print a traceback.
        print("\nInterrupted before scraping started, nothing was saved.", flush=True)
        sys.exit(130)
