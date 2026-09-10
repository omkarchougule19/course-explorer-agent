"""
repair.py

The Repair step: given the original question, the SQL that failed, and the
Critic's feedback (schema errors, an execution error, or an intent problem),
produce a corrected query. One LLM call per attempt. The 2-attempt cap and
the explicit-failure fallback live in graph.py - this module just does one
repair.
"""

import re
from dataclasses import dataclass

from app.sql_pipeline.schema_text import system_context

_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

_REPAIR_PROMPT = """{system}

---
You are the repair step of a SQL pipeline. A previous attempt to answer the
question below produced a query that was rejected. Write a corrected
SQLite-compatible SELECT query that fixes the stated problem and still
follows every rule above.

Question: {question}

Rejected SQL:
{sql}

Why it was rejected:
{feedback}

Available tables and their real columns:
{catalog}

{terms}

Respond with ONLY the corrected SELECT query - no prose, no comments, no
code fences."""


@dataclass
class Repair:
    sql: str
    raw: str


def _clean_sql(sql: str) -> str:
    sql = _FENCE_RE.sub("", (sql or "").strip()).strip()
    return sql.rstrip(";").strip()


def repair(llm, question: str, bad_sql: str, feedback: str, catalog_text: str,
           terms_note: str = "") -> Repair:
    prompt = _REPAIR_PROMPT.format(
        system=system_context(), question=question, sql=bad_sql,
        feedback=feedback, catalog=catalog_text, terms=terms_note,
    )
    resp = llm.invoke(prompt)
    text = getattr(resp, "content", resp)
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return Repair(sql=_clean_sql(str(text)), raw=str(text))
