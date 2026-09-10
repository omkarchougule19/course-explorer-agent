"""
citations.py

Builds the "Sources: …" footer appended to every assistant answer, so a
reader can see which dataset a fact came from and how stale it is.

Fully deterministic - it inspects the SQL the agent actually ran (captured
by SQLCapture, a LangChain callback) and whether the semantic-search tool
fired. No extra LLM call. Disable with ANSWER_CITATIONS=0.
"""

import os
import re
from functools import lru_cache

import sqlglot
from sqlglot import exp

from app import db


def _enabled() -> bool:
    return os.environ.get("ANSWER_CITATIONS", "1").strip().lower() not in ("0", "false", "no", "")

# catalog table -> short human source label
TABLE_SOURCES = {
    "sections": "UIUC Course Explorer",
    "meetings": "UIUC Course Explorer",
    "grade_distributions": "grade distributions (wadefagen/datasets)",
    "teachers_ranked_excellent": "Ranked as Excellent by Students (wadefagen/datasets)",
    "gen_ed_categories": "Gen Ed list (wadefagen/datasets)",
    "academic_calendar": "UIUC Registrar academic calendar",
    "prerequisites": "prerequisites parsed from UIUC catalog descriptions",
}
_COURSE_EXPLORER = {"sections", "meetings"}
_RAG_LABEL = "UIUC course catalog descriptions"

_SUBJECT_RE = re.compile(r"\bsubject\s*(?:=|IN)\s*\(?\s*'([A-Z]{2,4})'", re.IGNORECASE)


from langchain_core.callbacks import BaseCallbackHandler


def _unwrap_query(raw: str) -> str:
    """Tool-calling agents hand the SQL tool its arg as a stringified dict -
    `{"query": "SELECT …"}` (JSON) or `{'query': "SELECT …"}` (Python repr).
    Pull the query out of either; otherwise return `raw` as-is."""
    if not raw.startswith("{"):
        return raw
    import ast
    import json
    for parse in (json.loads, ast.literal_eval):
        try:
            v = parse(raw)
            if isinstance(v, dict) and "query" in v:
                return str(v["query"])
        except Exception:
            continue
    return raw


class SQLCapture(BaseCallbackHandler):
    """LangChain callback: records every sql_db_query the agent runs and
    whether course_content_search fired."""

    def __init__(self):
        super().__init__()
        self.queries: list[str] = []
        self.rag_used = False

    def on_tool_start(self, serialized, input_str, **kwargs):
        name = ""
        if isinstance(serialized, dict):
            name = serialized.get("name", "")
        name = name or kwargs.get("name", "") or ""
        raw = input_str if isinstance(input_str, str) else str(input_str)
        if "course_content_search" in name:
            self.rag_used = True
            return
        if "sql_db_query" in name or "select" in raw.lower():
            q = _unwrap_query(raw.strip())
            if q:
                self.queries.append(q)


def tables_in(sql_texts) -> set[str]:
    """Real catalog tables referenced across one or more SQL strings."""
    dialect = "postgres" if db.is_postgres() else "sqlite"
    found: set[str] = set()
    for sql in sql_texts or []:
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:
            continue
        if tree is None:
            continue
        for t in tree.find_all(exp.Table):
            name = (t.name or "").lower()
            if name in TABLE_SOURCES:
                found.add(name)
    return found


def _subjects_in(sql_texts) -> set[str]:
    out: set[str] = set()
    for sql in sql_texts or []:
        out.update(m.upper() for m in _SUBJECT_RE.findall(sql or ""))
    return out


@lru_cache(maxsize=1)
def _freshness_map() -> dict:
    """{subject: 'YYYY-MM-DD'} last scrape per subject, plus '' -> newest
    overall. Cached: sync dates only move on a manual re-scrape."""
    try:
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT subject, MAX(scraped_at) AS m FROM sections GROUP BY subject"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    newest = ""
    for r in rows:
        d = str(r["m"] or "")[:10]
        if d:
            out[r["subject"]] = d
            newest = max(newest, d)
    out[""] = newest
    return out


def _course_explorer_label(sql_texts) -> str:
    fm = _freshness_map()
    if not fm:
        return "UIUC Course Explorer"
    subjects = sorted(s for s in _subjects_in(sql_texts) if s in fm)
    if len(subjects) == 1:
        return f"UIUC Course Explorer ({subjects[0]} synced {fm[subjects[0]]})"
    if 2 <= len(subjects) <= 3:
        return "UIUC Course Explorer (" + ", ".join(f"{s} {fm[s]}" for s in subjects) + ")"
    newest = fm.get("", "")
    return f"UIUC Course Explorer (last sync {newest})" if newest else "UIUC Course Explorer"


def sources_footer(sql_texts, rag_used: bool = False, question: str = "") -> str:
    """The trailing '\\n\\n---\\n*Sources: …*' line, or '' when nothing was
    queried (refusals, setup errors)."""
    if not _enabled():
        return ""
    tables = tables_in(sql_texts)
    if not tables and not rag_used:
        return ""

    labels: list[str] = []
    if tables & _COURSE_EXPLORER:
        labels.append(_course_explorer_label(sql_texts))
    if rag_used:
        labels.append(_RAG_LABEL)
    for t in sorted(tables - _COURSE_EXPLORER):
        labels.append(TABLE_SOURCES[t])

    # de-dup, keep order
    seen, ordered = set(), []
    for lb in labels:
        if lb not in seen:
            seen.add(lb)
            ordered.append(lb)
    if not ordered:
        return ""
    return "\n\n---\n*Sources: " + "; ".join(ordered) + ".*"
