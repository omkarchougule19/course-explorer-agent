"""
critique.py

The Critic. Two independent checks on a generated SQL query:

1. Static schema check (no LLM, deterministic): parse the SQL with sqlglot
   and confirm every table and every qualified column it references really
   exists in the live catalog (catalog.py). This is what catches a
   hallucinated `sections.professor` or `FROM course_reviews` - the single
   failure mode the whole Critic/Repair loop was built to measure and
   reduce.

2. Intent check (one cheap LLM call): does the query's *structure* actually
   answer the question that was asked - right entity, right filters
   (term/semester/subject), aggregate vs. row list? Deliberately
   conservative: it only reports "flawed" when it is confident the query is
   wrong, because a false "flawed" costs a wasted repair attempt.

`critique()` runs both and returns a combined verdict plus feedback text the
Repair step can act on.
"""

import json
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.sql_pipeline.catalog import catalog_signature


# --------------------------------------------------------------------------
# 1. Static schema check
# --------------------------------------------------------------------------

@dataclass
class StaticResult:
    ok: bool
    unknown_tables: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)  # "table.column"
    parse_error: str | None = None

    @property
    def message(self) -> str:
        if self.parse_error:
            return f"The SQL did not parse: {self.parse_error}"
        bits = []
        if self.unknown_tables:
            bits.append("references table(s) that do not exist: "
                        + ", ".join(self.unknown_tables))
        if self.unknown_columns:
            bits.append("references column(s) that do not exist: "
                        + ", ".join(self.unknown_columns))
        return "; ".join(bits) if bits else "schema check passed"


def _alias_to_table(tree: exp.Expression) -> dict[str, str]:
    """alias -> real table name, for every aliased table reference."""
    out: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        if t.alias:
            out[t.alias.lower()] = t.name.lower()
    return out


def _cte_names(tree: exp.Expression) -> set[str]:
    return {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}


def _output_aliases(tree: exp.Expression) -> set[str]:
    """Names introduced by `SELECT expr AS name` - legal to reference in
    GROUP BY / ORDER BY / HAVING and not columns to validate."""
    out: set[str] = set()
    for select in tree.find_all(exp.Select):
        for e in select.expressions:
            if isinstance(e, exp.Alias) and e.alias:
                out.add(e.alias.lower())
    return out


def static_schema_check(sql: str, catalog: dict[str, "frozenset[str] | set[str]"],
                        dialect: str) -> StaticResult:
    """Deterministic check of `sql` against `catalog` ({table: {column}}).

    Conservative by construction: an unqualified column is only flagged when
    the statement has no CTE or subquery that could legitimately supply it,
    and anything that resolves to a CTE/subquery alias is skipped rather than
    guessed at. The goal is zero false positives on valid SQL, accepting that
    a few exotic hallucinations in heavily-nested queries slip through."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # sqlglot.errors.ParseError and friends
        return StaticResult(ok=False, parse_error=str(exc).splitlines()[0][:200])
    if tree is None:
        return StaticResult(ok=False, parse_error="empty statement")

    catalog = {t.lower(): {c.lower() for c in cols} for t, cols in catalog.items()}
    ctes = _cte_names(tree)
    alias_map = _alias_to_table(tree)
    out_aliases = _output_aliases(tree)
    has_derived = bool(ctes) or any(True for _ in tree.find_all(exp.Subquery))

    # -- tables --
    referenced_tables = {t.name.lower() for t in tree.find_all(exp.Table)}
    unknown_tables = sorted(
        t for t in referenced_tables if t not in catalog and t not in ctes
    )

    # columns available across every *real* table named in the query
    real_tables = [t for t in referenced_tables if t in catalog]
    all_real_cols: set[str] = set()
    for t in real_tables:
        all_real_cols |= catalog[t]

    # -- columns --
    unknown_columns: list[str] = []
    for col in tree.find_all(exp.Column):
        name = (col.name or "").lower()
        if not name or name == "*":
            continue
        qualifier = (col.table or "").lower()
        if qualifier:
            resolved = alias_map.get(qualifier, qualifier)
            if resolved in catalog:
                if name not in catalog[resolved]:
                    unknown_columns.append(f"{resolved}.{name}")
            # else: qualifier is a CTE / subquery / already-flagged table -> skip
        else:
            if name in out_aliases or name in all_real_cols:
                continue
            if has_derived:
                continue  # could come from a CTE/subquery - don't guess
            unknown_columns.append(name)

    unknown_columns = sorted(set(unknown_columns))
    ok = not unknown_tables and not unknown_columns
    return StaticResult(ok=ok, unknown_tables=unknown_tables,
                        unknown_columns=unknown_columns)


# --------------------------------------------------------------------------
# 2. Intent check (LLM)
# --------------------------------------------------------------------------

_INTENT_PROMPT = """You are a SQL reviewer for a UIUC course-catalog database.
Given the schema, a user question, and a candidate SQL query, decide only ONE
thing: does this query, if run, actually answer the question that was asked?

Judge intent and structure, NOT syntax or whether column names exist (a
separate deterministic checker handles that). Look for:
- wrong entity (question asks about courses, query returns sections, etc.)
- missing or wrong filter (term/year, semester casing, subject code)
- aggregate vs. list mismatch (question wants a count, query lists rows)
- a join that links the wrong keys

Be conservative. Only answer "flawed" when you are confident the query is
wrong. If it is plausibly correct, or the question is ambiguous, answer "ok".

Schema:
{schema}

Question: {question}

SQL:
{sql}

Respond with ONLY a JSON object: {{"verdict": "ok" | "flawed", "issue": "<one sentence, empty if ok>"}}"""


def _parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, issue). Fail-open to ('ok', '') on any parse trouble - the
    Critic must never block every query because the reviewer's JSON was
    malformed."""
    try:
        lo, hi = text.find("{"), text.rfind("}")
        obj = json.loads(text[lo:hi + 1]) if 0 <= lo < hi else {}
        verdict = str(obj.get("verdict", "ok")).strip().lower()
        if verdict not in ("ok", "flawed"):
            verdict = "ok"
        return verdict, str(obj.get("issue", "")).strip()
    except Exception:
        return "ok", ""


def intent_check(llm, schema: str, question: str, sql: str) -> tuple[str, str]:
    """('ok'|'flawed', issue_text). Any LLM error fails open to ('ok', '')."""
    try:
        resp = llm.invoke(_INTENT_PROMPT.format(schema=schema, question=question, sql=sql))
        text = getattr(resp, "content", resp)
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return _parse_verdict(str(text))
    except Exception:
        return "ok", ""


# --------------------------------------------------------------------------
# Combined
# --------------------------------------------------------------------------

@dataclass
class Critique:
    ok: bool
    static: StaticResult
    intent_verdict: str          # "ok" | "flawed"
    intent_issue: str
    hallucinated_refs: bool      # static check found a bad table/column

    @property
    def feedback(self) -> str:
        """Actionable text for the Repair step."""
        parts = []
        if not self.static.ok:
            parts.append(f"Schema check: {self.static.message}.")
        if self.intent_verdict == "flawed" and self.intent_issue:
            parts.append(f"Intent check: {self.intent_issue}")
        return " ".join(parts) or "No issues found."


def critique(sql: str, question: str, llm, catalog: dict, dialect: str,
             run_intent: bool = True, terms_note: str = "") -> Critique:
    static = static_schema_check(sql, catalog, dialect)
    hallucinated = bool(static.unknown_tables or static.unknown_columns)
    # Skip the LLM call when the query is already known-broken - it will be
    # repaired regardless, and when there's nothing structurally there to
    # review.
    if run_intent and static.parse_error is None:
        schema = catalog_signature(catalog)
        if terms_note:
            schema = f"{schema}\n\n{terms_note}"
        verdict, issue = intent_check(llm, schema, question, sql)
    else:
        verdict, issue = "ok", ""
    ok = static.ok and verdict == "ok"
    return Critique(ok=ok, static=static, intent_verdict=verdict,
                    intent_issue=issue, hallucinated_refs=hallucinated)
