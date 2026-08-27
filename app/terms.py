"""
terms.py

Shared definition of which terms this app keeps historical data for: the
current term, the two terms before it, and the two terms after it, walking
UIUC's spring/summer/fall cycle (winter intersession isn't part of the
rotation - it wasn't in the window the user asked for). Used by
load_grades.py and load_tre.py so they don't hoard two decades of history
that was never needed - only the window around "now" that's actually useful
for a low-traffic course lookup/RAG app.

Update CURRENT_YEAR/CURRENT_SEMESTER below as terms roll forward. This
project is already operated manually/monthly (scraper.py takes explicit
--year/--semester per run), so a small manual edit here fits the same
operating model rather than an auto-detected value with date-boundary edge
cases to get wrong.
"""

SEMESTER_CYCLE = ["spring", "summer", "fall"]

CURRENT_YEAR = 2026
CURRENT_SEMESTER = "fall"


def active_terms(
    current_year: int = CURRENT_YEAR,
    current_semester: str = CURRENT_SEMESTER,
    back: int = 2,
    forward: int = 2,
) -> list[tuple[int, str]]:
    """Ordered (year, semester) tuples from `back` terms before the current term
    through `forward` terms after it, inclusive, walking the spring/summer/fall
    cycle. Defaults to a 5-term window: 2 before, current, 2 after."""
    idx = current_year * 3 + SEMESTER_CYCLE.index(current_semester)
    terms = []
    for offset in range(-back, forward + 1):
        i = idx + offset
        year, sem_idx = divmod(i, 3)
        terms.append((year, SEMESTER_CYCLE[sem_idx]))
    return terms


ACTIVE_TERMS = active_terms()
ACTIVE_TERM_KEYS = {f"{y}-{s}" for y, s in ACTIVE_TERMS}
