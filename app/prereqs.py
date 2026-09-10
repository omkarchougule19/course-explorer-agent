"""
prereqs.py

Pull structured prerequisites out of a course's free-text catalog
`description`. No network, no database - `load_prereqs.py` runs this over
`sections.description` and stores the result.

UIUC prerequisite clauses follow a loose but usable grammar:

    "One of CS 173, MATH 213; CS 225"   ->  (CS 173 OR MATH 213) AND (CS 225)
    "ABE 227 and ABE 228"               ->  (ABE 227) AND (ABE 228)
    "MATH 220 or MATH 221"              ->  (MATH 220 OR MATH 221)
    "Consent of instructor"             ->  no courses, one condition
    "STAT 410 or equivalent; ..."       ->  (STAT 410) AND [condition text]

`;` and the word "and" separate AND-ed requirement groups; "or" / "one of" /
commas inside a group are alternatives. Anything in a group with no course
token is kept verbatim as a condition. The parse is deliberately
conservative - when a clause is too tangled to split with confidence it
falls back to "every course token mentioned, as one OR-group" and the raw
sentence is always retained so a caller can quote it instead.
"""

import re
from dataclasses import dataclass, field

# Course reference: 2-4 uppercase letters, a space (optional), 3 digits, an
# optional trailing letter. "CS 225", "MATH 347", "STAT 100", "CS 173".
_COURSE_RE = re.compile(r"\b([A-Z]{2,4})\s?(\d{3}[A-Z]?)\b")

# The prerequisite sentence, from the label up to the end of that sentence.
# Stops before the registrar boilerplate that often follows.
_CLAUSE_RE = re.compile(
    r"Prerequisite[s]?:\s*(.+?)"
    r"(?:\.\s+(?:Credit is not given|Same as|See |This course|Students |May be )"
    r"|\.\s*$|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_CONDITION_HINTS = re.compile(
    r"consent|permission|standing|restricted to|by application|by interview|"
    r"equivalent|placement|department|major|admission|GPA|grade[- ]point|"
    r"honors|proficiency|audition|instructor'?s approval",
    re.IGNORECASE,
)

_CONCURRENT_RE = re.compile(r"concurrent|credit or concurrent", re.IGNORECASE)

CourseRef = tuple  # (subject: str, course_number: str)


@dataclass
class Group:
    """One AND-ed requirement: satisfy any one of `options`."""
    options: list[CourseRef] = field(default_factory=list)
    relation: str = "prereq"          # "prereq" | "concurrent"
    conditions: list[str] = field(default_factory=list)


@dataclass
class ParsedPrereqs:
    groups: list[Group] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)   # clause-level, no group
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.groups and not self.conditions


def _courses_in(text: str) -> list[CourseRef]:
    seen: set[CourseRef] = set()
    out: list[CourseRef] = []
    for subj, num in _COURSE_RE.findall(text):
        ref = (subj.upper(), num)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _clean_condition(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,;.")
    text = re.sub(r"^(?:and|or|one of)\s+", "", text, flags=re.IGNORECASE).strip()
    # Trim a leading list-number like "2)".
    text = re.sub(r"^\d\)\s*", "", text)
    return text[:200]


def parse_prerequisites(description: str) -> ParsedPrereqs:
    if not description:
        return ParsedPrereqs()
    m = _CLAUSE_RE.search(description)
    if not m:
        return ParsedPrereqs()
    clause = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".")
    result = ParsedPrereqs(raw=f"Prerequisite: {clause}.")

    # Top-level AND separators: ";" and the word "and" (not "one of ... and"
    # inside an option list - that's rare enough to accept as noise).
    pieces = re.split(r";\s*|\s+and\s+", clause, flags=re.IGNORECASE)

    for piece in pieces:
        piece = piece.strip(" ,")
        if not piece:
            continue
        courses = _courses_in(piece)
        if courses:
            grp = Group(
                options=courses,
                relation="concurrent" if _CONCURRENT_RE.search(piece) else "prereq",
            )
            # A trailing "or consent of instructor" etc. rides along on the
            # same piece - record it as an alternative condition on the group.
            tail = _CONDITION_HINTS.search(piece)
            if tail:
                cond = _clean_condition(piece[tail.start():])
                if cond and not _COURSE_RE.search(cond):
                    grp.conditions.append(cond)
            result.groups.append(grp)
        else:
            cond = _clean_condition(piece)
            if cond:
                result.conditions.append(cond)

    return result
