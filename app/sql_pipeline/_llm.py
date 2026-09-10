"""
_llm.py

Small shared helpers for the LLM-calling steps. Each of these was repeated
verbatim in the generate / repair / synthesize / critique modules:

- ask_text     - invoke a chat model and get its reply as a plain string,
                 flattening the list-of-parts content shape some providers
                 return.
- clean_sql    - strip ``` fences and a trailing semicolon off a generated
                 query.
- loads_lenient - pull the first {...} block out of a model reply and parse
                 it, returning {} on any failure (models wrap JSON in prose).
"""

import json
import re

_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def ask_text(llm, prompt: str) -> str:
    resp = llm.invoke(prompt)
    text = getattr(resp, "content", resp)
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return str(text)


def clean_sql(sql: str) -> str:
    sql = _FENCE_RE.sub("", (sql or "").strip()).strip()
    return sql.rstrip(";").strip()


def loads_lenient(text: str) -> dict:
    try:
        lo, hi = text.find("{"), text.rfind("}")
        return json.loads(text[lo:hi + 1]) if 0 <= lo < hi else {}
    except Exception:
        return {}
