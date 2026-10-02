"""
searchfields.py

Columns that make the catalog's free text matchable the way students write
it, so the model filters on a plain column instead of guessing a text shape
(found by the query-gap critic, 2026-10-01):

  sections.instructor_last / instructor_initial
      instructor is stored 'Last, F'. Students write "Heng Ji", "O'Brien",
      "Prof Fagen-Ulmschneider". instructor_last is the last name lowercased
      with everything but letters removed ("obrien", "fagenulmschneider");
      instructor_initial is the lowercase first initial, or NULL. 430 last
      names belong to more than one instructor, so a match is never proof of
      a person.

  sections.title_search
      course_label is a 30-character short title ("Intro Computing: Engrg &
      Sci"). title_search is it lowercased, '&' as 'and', with each
      unambiguous abbreviation followed by its expansion ("intro introduction
      computing engrg engineering and sci science"), so a search for
      "introduction" or "engineering" finds it. Ambiguous ones ("Comp",
      "Inst", "Med") stay as written. The map was built from the abbreviations
      that actually occur in the titles, by frequency.

  meetings.is_online
      1 when meeting_type starts with 'Online' ('Online', 'Online Lecture',
      'Online Lab', 'Online Discussion', ...: 459 of 3,240 online meetings
      weren't the bare 'Online'), else 0.

Used by scraper.save_sections for new rows and backfill_credits.py for old
ones. Pure functions, no I/O.
"""

import re
from typing import Optional, Tuple

_ABBREVIATIONS = {
    "intro": "introduction", "adv": "advanced", "comm": "communication",
    "psych": "psychology", "lit": "literature", "am": "american", "amer": "american",
    "tech": "technology", "lang": "language", "engrg": "engineering", "mgmt": "management",
    "mgt": "management", "sem": "seminar", "sci": "science", "chem": "chemistry",
    "info": "information", "res": "research", "hist": "history", "arch": "architecture",
    "ed": "education", "educ": "education", "intl": "international", "math": "mathematics",
    "env": "environmental", "envir": "environmental", "sys": "systems", "syst": "systems",
    "dev": "development", "ag": "agricultural", "econ": "economics", "anlys": "analysis",
    "anal": "analysis", "svcs": "services", "appl": "applied", "mech": "mechanics",
    "spch": "speech", "actv": "activity", "grp": "group", "grps": "groups",
    "instr": "instruction", "struct": "structures", "phys": "physics", "physcs": "physics",
    "wrkng": "working", "hlth": "health", "tchg": "teaching", "engl": "english",
    "mfg": "manufacturing", "orgs": "organizations", "org": "organization",
    "fund": "fundamentals", "prin": "principles", "stat": "statistics",
    "contemp": "contemporary", "elem": "elementary", "ckts": "circuits",
    "ai": "artificial intelligence", "ml": "machine learning",
}

SEARCH_COLUMN_TYPES_SECTIONS = (("instructor_last", "TEXT"), ("instructor_initial", "TEXT"),
                                ("title_search", "TEXT"))
SEARCH_COLUMNS_SECTIONS = tuple(c for c, _ in SEARCH_COLUMN_TYPES_SECTIONS)
SEARCH_COLUMN_TYPES_MEETINGS = (("is_online", "INTEGER"),)


def normalize_last_name(text: Optional[str]) -> Optional[str]:
    """'O'Brien' -> 'obrien', 'Fagen-Ulmschneider' -> 'fagenulmschneider',
    'Alves de Oliveira' -> 'alvesdeoliveira'; None for nothing usable."""
    letters = re.sub(r"[^a-z]", "", (text or "").lower())
    return letters or None


def instructor_fields(instructor: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(instructor_last, instructor_initial) from a stored 'Last, F'."""
    raw = (instructor or "").strip()
    if not raw or raw == "-":
        return None, None
    last, _, first = raw.partition(",")
    first = first.strip()
    initial = first[0].lower() if first[:1].isalpha() else None
    return normalize_last_name(last), initial


def title_search_text(course_label: Optional[str]) -> Optional[str]:
    """Lowercased title with '&' as 'and' and abbreviations expanded in place
    (the abbreviation is kept, so both spellings match)."""
    if not course_label:
        return None
    out = []
    for word in re.findall(r"[A-Za-z0-9]+|&", course_label.lower()):
        if word == "&":
            out.append("and")
            continue
        out.append(word)
        if word in _ABBREVIATIONS:
            out.append(_ABBREVIATIONS[word])
    return " ".join(out)


def section_search_fields(instructor: Optional[str], course_label: Optional[str]) -> tuple:
    """Values for SEARCH_COLUMNS_SECTIONS, in order."""
    return instructor_fields(instructor) + (title_search_text(course_label),)


def is_online(meeting_type: Optional[str]) -> int:
    return 1 if (meeting_type or "").strip().lower().startswith("online") else 0
