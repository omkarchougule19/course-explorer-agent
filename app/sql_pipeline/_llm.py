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


# Running total of tokens the provider reports for calls made through
# ask_text, so the eval harness can measure what each question costs against a
# daily token cap. reset_tokens() before a question, take_tokens() after.
_tokens_used = 0


def reset_tokens() -> None:
    global _tokens_used
    _tokens_used = 0


def take_tokens() -> int:
    return _tokens_used


def ask_text(llm, prompt: str) -> str:
    global _tokens_used
    resp = llm.invoke(prompt)
    usage = getattr(resp, "usage_metadata", None) or {}
    _tokens_used += int(usage.get("total_tokens") or 0)
    text = getattr(resp, "content", resp)
    if isinstance(text, list):
        # List-of-parts content: dict parts carry {"text": ...}; some providers
        # mix in bare strings. Keep both so nothing is silently dropped.
        parts = []
        for p in text:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text", "")))
        text = "".join(parts)
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
