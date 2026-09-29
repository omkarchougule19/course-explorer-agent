"""
credits.py

Turns the catalog's free-text credit facts into columns the assistant can
filter on directly, instead of parsing text inside every SQL query it writes:

    credit_hours "3 OR 4 hours."             -> credit_min 3, credit_max 4
    description  "... 3 undergraduate hours.
                  3 or 4 graduate hours. ..." -> grad_credit 'yes', grad_min 3, grad_max 4
    description  "... No graduate credit. ..." -> grad_credit 'no'
    description  "... Restricted to Theatre
                  majors and minors. ..."    -> restriction "Restricted to Theatre majors and minors."

Why: asked for "CS courses with 3 credits open to graduate students", the
model compared credit_hours to the number 3 (it's text, so nothing matched)
and treated only 500-level courses as graduate - most 400-level courses give
graduate credit, which only the description says.

grad_credit is 'yes', 'no', or NULL = the catalog doesn't say (about 15% of
400-level course-terms). 500-level and above are graduate courses, so they
default to 'yes' with credit_hours as the graduate hours; 100-300 level
default to 'no'. An explicit statement in the description always wins.

Used by scraper.save_sections (so new rows arrive parsed) and by
backfill_credits.py (for rows already stored). Pure functions, no I/O.
"""

import re
from typing import Optional, Tuple

_NUM = r"(\d+(?:\.\d+)?)"
# "3 hours." / "3 OR 4 hours." / "1 TO 4 hours." / "0.5 hours."
_HOURS_RE = re.compile(rf"^\s*{_NUM}(?:\s+(?:OR|TO|-)\s+{_NUM})?\s+hours?\.?\s*$", re.IGNORECASE)
# A sentence that is only a graduate-credit statement: "3 or 4 graduate hours."
# Anchored to the whole sentence, so "May be repeated to a maximum of 6
# undergraduate hours or 8 graduate hours." is not read as the course's credit.
_GRAD_HOURS_RE = re.compile(rf"^{_NUM}(?:\s+(?:or|to)\s+{_NUM})?\s+graduate hours?\.?$", re.IGNORECASE)
_NO_GRAD_RE = re.compile(r"^No graduate credit\.?$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)\s+")
_RESTRICTION_RE = re.compile(r"\b(restricted to|majors only|for majors)\b", re.IGNORECASE)
RESTRICTION_MAX_CHARS = 300

CREDIT_COLUMN_TYPES = (("credit_min", "REAL"), ("credit_max", "REAL"), ("grad_credit", "TEXT"),
                       ("grad_min", "REAL"), ("grad_max", "REAL"), ("restriction", "TEXT"))
CREDIT_COLUMNS = tuple(c for c, _ in CREDIT_COLUMN_TYPES)


def parse_hours(credit_hours: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """(min, max) credit hours from the credit_hours text; (None, None) if it
    isn't in a known shape."""
    m = _HOURS_RE.match(credit_hours or "")
    if not m:
        return None, None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return min(lo, hi), max(lo, hi)


def _sentences(description: Optional[str]):
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(description or "") if s.strip()]


def parse_graduate(course_number: Optional[str], description: Optional[str],
                   credit_min: Optional[float], credit_max: Optional[float]):
    """(grad_credit, grad_min, grad_max). An explicit catalog statement wins;
    otherwise the course level decides, and 400-level with no statement is
    unknown (None) rather than a guess."""
    for s in _sentences(description):
        if _NO_GRAD_RE.match(s):
            return "no", None, None
        m = _GRAD_HOURS_RE.match(s)
        if m:
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else lo
            return "yes", min(lo, hi), max(lo, hi)
    level = (course_number or "")[:1]
    if level.isdigit() and int(level) >= 5:
        return "yes", credit_min, credit_max
    if level in ("1", "2", "3"):
        return "no", None, None
    return None, None, None


def parse_restriction(description: Optional[str]) -> Optional[str]:
    """The description's enrollment-restriction sentence(s), verbatim."""
    found = [s for s in _sentences(description) if _RESTRICTION_RE.search(s)]
    if not found:
        return None
    text = " ".join(found)
    return text if len(text) <= RESTRICTION_MAX_CHARS else text[:RESTRICTION_MAX_CHARS].rsplit(" ", 1)[0] + "..."


def credit_fields(course_number: Optional[str], credit_hours: Optional[str],
                  description: Optional[str]) -> tuple:
    """Values for CREDIT_COLUMNS, in order."""
    lo, hi = parse_hours(credit_hours)
    grad, glo, ghi = parse_graduate(course_number, description, lo, hi)
    return lo, hi, grad, glo, ghi, parse_restriction(description)
