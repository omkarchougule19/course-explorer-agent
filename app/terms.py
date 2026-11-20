"""
terms.py

One place for "which term is it?", so nothing else in the app hard-codes a
year or semester. Everything here is worked out from today's date and/or the
terms actually present in the data, so rolling into a new term needs no code
edit - only a scrape.

Three related ideas:

- current_term(): the term in session today, from approximate UIUC
  boundaries (spring from Jan 1, summer from May 16, fall from Aug 16;
  winter intersession isn't part of the rotation). Used for the historical
  window that load_grades.py / load_tre.py keep (active_terms) and by
  sync_requests.py.
- planning_term(): the term students are registering for. Spring
  registration opens in late October, fall registration in early April, so
  from Oct 15 it is next spring, and from Apr 1 to Aug 15 it is this fall.
- default_term(available): what the site pre-selects - the planning term if
  it's in the data, else the current term, else the nearest term that is.

The date boundaries are approximate on purpose (a few days either side
doesn't matter for a default). If UIUC's calendar ever shifts enough to
matter, set CURRENT_TERM / DEFAULT_TERM (e.g. "spring 2027") in the
environment to pin them without a code change.
"""

import os
import re
from datetime import date

SEMESTER_CYCLE = ["spring", "summer", "fall"]

# (month, day) each term counts as "current" from.
_TERM_STARTS = (("spring", (1, 1)), ("summer", (5, 16)), ("fall", (8, 16)))
_SPRING_REGISTRATION = (10, 15)  # from here on, students plan next spring
_FALL_REGISTRATION = (4, 1)      # from here until fall starts, students plan fall

_LABEL_RE = re.compile(r"^\s*(?:(spring|summer|fall)[\s_-]+(\d{4})|(\d{4})[\s_-]+(spring|summer|fall))\s*$", re.IGNORECASE)


def parse_term(label: str) -> tuple[int, str]:
    """'fall 2026', '2026-fall', '2026 fall' -> (2026, 'fall')."""
    m = _LABEL_RE.match(label or "")
    if not m:
        raise ValueError(f"not a term: {label!r} (expected e.g. 'fall 2026')")
    sem = (m.group(1) or m.group(4)).lower()
    year = int(m.group(2) or m.group(3))
    return year, sem


def term_label(year: int, semester: str) -> str:
    """(2026, 'fall') -> 'fall 2026', the form the API and the agent use."""
    return f"{semester} {year}"


def term_index(year: int, semester: str) -> int:
    """A sortable position on the spring/summer/fall cycle."""
    return year * 3 + SEMESTER_CYCLE.index(semester)


def shift_term(year: int, semester: str, n: int) -> tuple[int, str]:
    """The term `n` steps after (n<0: before) the given one."""
    y, s = divmod(term_index(year, semester) + n, 3)
    return y, SEMESTER_CYCLE[s]


def _today(today: "date | None") -> date:
    return today or date.today()


def _env_term(name: str) -> "tuple[int, str] | None":
    value = os.environ.get(name, "").strip()
    return parse_term(value) if value else None


def current_term(today: "date | None" = None) -> tuple[int, str]:
    """The term in session on `today` (default: now). CURRENT_TERM overrides."""
    pinned = _env_term("CURRENT_TERM")
    if pinned:
        return pinned
    d = _today(today)
    current = "spring"
    for sem, (month, day) in _TERM_STARTS:
        if (d.month, d.day) >= (month, day):
            current = sem
    return d.year, current


def planning_term(today: "date | None" = None) -> tuple[int, str]:
    """The term students are registering for on `today`."""
    d = _today(today)
    md = (d.month, d.day)
    if md >= _SPRING_REGISTRATION:
        return d.year + 1, "spring"
    if _FALL_REGISTRATION <= md < _TERM_STARTS[2][1]:
        return d.year, "fall"
    return current_term(d)


def relative_terms(today: "date | None" = None) -> dict:
    """What students mean by relative term words on `today`, as labels:
    'this' (the current term), 'next' (the next fall or spring - students
    rarely mean summer by "next semester"), and 'next spring' / 'next summer'
    / 'next fall' (that season's first term after the current one)."""
    cy, cs = current_term(today)
    out = {"this": term_label(cy, cs)}
    n = 1
    while True:
        y, s = shift_term(cy, cs, n)
        if s != "summer":
            out["next"] = term_label(y, s)
            break
        n += 1
    for season in SEMESTER_CYCLE:
        for n in range(1, 4):
            y, s = shift_term(cy, cs, n)
            if s == season:
                out[f"next {season}"] = term_label(y, s)
                break
    return out


def default_term(available, today: "date | None" = None) -> "str | None":
    """The term label to pre-select, chosen from `available` labels (or
    (year, semester) pairs): DEFAULT_TERM if set and present, else the
    planning term, else the current term, else the available term nearest
    the planning term (later wins a tie). None if nothing is available."""
    terms = {}
    for t in available or ():
        year, sem = parse_term(t) if isinstance(t, str) else (int(t[0]), str(t[1]).lower())
        if sem in SEMESTER_CYCLE:
            terms[(year, sem)] = term_label(year, sem)
    if not terms:
        return None
    pinned = _env_term("DEFAULT_TERM")
    for want in (pinned, planning_term(today), current_term(today)):
        if want and want in terms:
            return terms[want]
    target = term_index(*planning_term(today))
    nearest = min(terms, key=lambda t: (abs(term_index(*t) - target), -term_index(*t)))
    return terms[nearest]


def active_terms(
    current_year: "int | None" = None,
    current_semester: "str | None" = None,
    back: int = 2,
    forward: int = 2,
) -> list[tuple[int, str]]:
    """Ordered (year, semester) tuples from `back` terms before the current term
    through `forward` terms after it, inclusive. Defaults to a 5-term window
    around current_term()."""
    if current_year is None or current_semester is None:
        current_year, current_semester = current_term()
    return [shift_term(current_year, current_semester, n) for n in range(-back, forward + 1)]


# Computed once at import - fine for the CLI loaders that use them. A
# long-running process (the API) calls the functions above per request instead.
CURRENT_YEAR, CURRENT_SEMESTER = current_term()
ACTIVE_TERMS = active_terms()
ACTIVE_TERM_KEYS = {f"{y}-{s}" for y, s in ACTIVE_TERMS}
