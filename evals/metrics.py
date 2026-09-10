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


def answer_mentions(answer: str, needles: "list[str] | None") -> bool:
    """True if every needle appears in `answer` (case-insensitive). Empty /
    missing needle list -> True (nothing to check)."""
    if not needles:
        return True
    low = (answer or "").lower()
    return all(n.lower() in low for n in needles)


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
