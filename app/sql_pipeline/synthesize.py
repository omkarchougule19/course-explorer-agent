"""
synthesize.py

The final step: turn the executed query's rows into a natural-language
answer, following the same formatting rules the production agent uses
(prose or a short list by default, a Markdown table only for genuinely
tabular multi-row results, state the row count, name the term defaulted to).

Kept separate from generation so the eval harness can score the SQL result
set independently of the prose.
"""

import json

from app.agent import SYSTEM_CONTEXT

_MAX_ROWS_TO_LLM = 50

_SYNTH_PROMPT = """{system}

---
Answer the question below using ONLY the query result rows provided. Do not
invent values. If the result set is empty, say so plainly - the data may
simply not be published yet for that term.

Question: {question}

SQL that was run:
{sql}

Result rows ({n} total{truncated}):
{rows}

Write the answer now, following the formatting rules above."""


def synthesize(llm, question: str, sql: str, rows: list[dict]) -> str:
    shown = rows[:_MAX_ROWS_TO_LLM]
    truncated = f", showing first {_MAX_ROWS_TO_LLM}" if len(rows) > _MAX_ROWS_TO_LLM else ""
    rows_text = json.dumps(shown, default=str, indent=2) if shown else "(no rows)"
    prompt = _SYNTH_PROMPT.format(
        system=SYSTEM_CONTEXT, question=question, sql=sql,
        n=len(rows), truncated=truncated, rows=rows_text,
    )
    resp = llm.invoke(prompt)
    text = getattr(resp, "content", resp)
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return str(text).strip()
