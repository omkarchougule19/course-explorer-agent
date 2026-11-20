"""
metrics.py

Scoring helpers for the eval harness. Kept separate from run.py so the
comparison logic (what counts as a result match, what reads as a refusal)
can be reasoned about and adjusted on its own.

Result matching is intentionally two-tier:

  strict - the candidate query returns exactly the same set of row tuples as
           the gold query.
  loose  - every value the gold query returns appears somewhere in the
           candidate's rows, and the candidate isn't wildly larger. This is
           the headline "did it get the right answer" signal: it forgives
           `SELECT DISTINCT instructor, crn` vs. a gold `SELECT instructor`,
           and `COUNT(DISTINCT crn)` vs. `COUNT(*)`, while still failing a
           query that lists rows when the question wanted a count.
"""

import re

_WS_RE = re.compile(r"\s+")


def normalize_cell(v) -> str:
    if v is None:
        return ""
    return _WS_RE.sub(" ", str(v).strip().lower())


def value_set(rows: "list[dict] | None") -> set[str]:
    """Every cell value in `rows`, normalized and flattened into one set."""
    out: set[str] = set()
    for row in rows or []:
        for v in row.values():
            out.add(normalize_cell(v))
    out.discard("")
    return out


def tuple_set(rows: "list[dict] | None") -> set[tuple]:
    return {tuple(normalize_cell(v) for v in row.values()) for row in (rows or [])}


def result_match(gold_rows: "list[dict] | None",
                 cand_rows: "list[dict] | None") -> dict:
    """{'strict': bool, 'loose': bool}. Both True when gold and candidate are
    both empty (the expected shape for a 'no data' question with a gold query
    that legitimately returns nothing)."""
    gold_rows = gold_rows or []
    cand_rows = cand_rows or []
    if not gold_rows and not cand_rows:
        return {"strict": True, "loose": True}
    if not gold_rows or cand_rows is None:
        return {"strict": False, "loose": False}

    strict = tuple_set(gold_rows) == tuple_set(cand_rows)

    gv, cv = value_set(gold_rows), value_set(cand_rows)
    subset = bool(gv) and gv.issubset(cv)
    not_bloated = len(cand_rows) <= max(len(gold_rows) * 1.5 + 3, len(gold_rows) + 3)
    loose = strict or (subset and not_bloated)
    return {"strict": strict, "loose": loose}


# gpt-oss writes "CS 440" and "4‑credit": narrow/no-break spaces and
# non-breaking hyphens. Fold them so "CS 440" matches what a reader sees.
_FOLD = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " ",
                       "‐": "-", "‑": "-", "‒": "-", "–": "-"})


def fold(text: str) -> str:
    return (text or "").translate(_FOLD).lower()


def _has(low: str, needle: str) -> bool:
    # "Oct 16|October 16" = any one of the alternatives.
    return any(fold(alt) in low for alt in needle.split("|"))


def answer_mentions(answer: str, needles: "list[str] | None") -> bool:
    """True if every needle appears in `answer` (case-insensitive; a needle
    may list alternatives separated by '|'). Empty / missing needle list ->
    True (nothing to check)."""
    if not needles:
        return True
    low = fold(answer)
    return all(_has(low, n) for n in needles)


def answer_excludes(answer: str, needles: "list[str] | None") -> bool:
    """True if no needle appears in `answer` (case-insensitive) - for what an
    answer must NOT say: a verdict, an invented title, a section dump."""
    if not needles:
        return True
    low = fold(answer)
    return not any(_has(low, n) for n in needles)


def answer_covers_rows(answer: str, rows, max_rows: int = 30) -> bool:
    """For an answer built from a tool rather than SQL (course_facts), where
    there are no candidate rows to match: True if every cell of every gold
    row appears in the answer. Too many gold rows -> False."""
    if not rows or len(rows) > max_rows:
        return False
    low = fold(answer)
    words = set(re.findall(r"[a-z0-9]+", low))
    for row in rows:
        cells = row.values() if isinstance(row, dict) else row
        for v in cells:
            # Word-level, so the stored "4 hours." matches "4 credit hours".
            if v is not None and not set(re.findall(r"[a-z0-9]+", str(v).lower())) <= words:
                return False
    return True


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK_RE = re.compile(r"^0?(\d{1,2}):(\d{2})\s*([AP]M)?$", re.IGNORECASE)


def value_needle(v) -> str:
    """A stored value as an answer_mentions needle, with the ways an answer
    writes it: '2026-10-16' -> 'October 16|Oct 16|Oct. 16|10/16|10-16',
    '03:30PM' -> '3:30'. Lets eval items take their expected facts from a
    query (answer_contains_sql) instead of strings frozen from one term."""
    s = str(v).strip()
    m = _ISO_DATE_RE.match(s)
    if m:
        month, day = _MONTHS[int(m.group(2)) - 1], int(m.group(3))
        mm = int(m.group(2))
        return "|".join((f"{month} {day}", f"{month[:3]} {day}", f"{month[:3]}. {day}",
                         f"{mm}/{day}", f"{mm}-{day}", s))
    m = _CLOCK_RE.match(s)
    if m:
        return f"{int(m.group(1))}:{m.group(2)}"
    return s


def sql_needles(rows, mode: str = "all") -> "list[str]":
    """Needles from a query's rows for answer_mentions / answer_excludes:
    'all' = one needle per value (every value must appear), 'any' = one
    needle joining them all (at least one must)."""
    values = []
    for row in rows or ():
        cells = row.values() if isinstance(row, dict) else row
        values += [value_needle(v) for v in cells if v not in (None, "")]
    values = list(dict.fromkeys(values))
    if mode == "any":
        return ["|".join(values)] if values else []
    return values


_REFUSAL_MARKERS = (
    "only answer questions", "only able to answer", "can only help with",
    "can only answer", "out of scope", "outside what i can", "can't comply",
    "cannot comply", "not a general-purpose assistant", "don't have information about",
    "i can't help with that", "i cannot help with that", "unable to help with",
    "isn't something i can", "is outside", "not something i can answer",
)

_NO_DATA_MARKERS = (
    "no data", "not available", "isn't published", "not published",
    "not been published", "no grade", "no rating", "don't have", "do not have",
    "does not have", "doesn't have", "no records", "no record", "no information",
    "not in the", "isn't in the", "not tracked", "not recorded", "no such data",
    "couldn't find", "could not find", "no waitlist", "not have grade",
    "not have rating", "no prerequisite", "isn't tracked", "no rows",
    "no results", "no matching", "no one", "nobody", "none ", "no teacher",
    "no faculty", "no entries", "not currently", "isn't any", "aren't any",
    "there are no", "there is no", "no listed", "not listed", "haven't been",
    "hasn't been", "yet to be", "not yet",
)


def looks_like_refusal(answer: str) -> bool:
    low = (answer or "").lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def looks_like_no_data(answer: str) -> bool:
    low = (answer or "").lower()
    return any(m in low for m in _NO_DATA_MARKERS)


def mean(xs: "list[bool | float | int]") -> "float | None":
    xs = list(xs)
    return round(sum(float(x) for x in xs) / len(xs), 4) if xs else None
