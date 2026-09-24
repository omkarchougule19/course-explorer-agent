"""
graph.py

LangGraph wiring of the Generator -> Critic -> Repair pipeline.

    generate --out of scope--> (refuse) --> END
    generate --> critic
    critic   --ok--> execute
    critic   --flaw, repairs left--> repair --> critic
    critic   --flaw, repairs exhausted--> finalize
    execute  --rows--> synthesize --> END
    execute  --db error, repairs left--> repair --> critic
    execute  --db error, repairs exhausted--> finalize
    repair   --same SQL as a prior attempt--> finalize   (cycle-breaker)
    finalize --> END

`finalize` ships the first repair candidate that executed cleanly if there
is one, and only returns an explicit failure string when nothing ever ran.

The Critic runs two checks (critique.py). The deterministic schema check
runs on every pass. The LLM intent-check is, by default, a *repair verifier*
only - it never vetoes the generator's first query, just judges whether a
repair improved things (SQL_PIPELINE_INTENT_CHECK = off | repair | always).
See evals/FINDINGS.md for why: as a first-pass gate the intent-check
manufactured objections to correct queries more often than it caught real
errors.

`mode`:
    "critic"   - full loop above
    "baseline" - generate -> execute -> synthesize, no critic, no repair
                 (this is the control arm for the evals)

Public entry point: run_pipeline(question, mode, history) -> PipelineResult.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app import agent as agent_mod
from app import db, sql_guard
from app.sql_pipeline import catalog as catalog_mod
from app.sql_pipeline.critique import Critique, critique, static_schema_check
from app.sql_pipeline.steps import generate, repair, synthesize

MAX_REPAIRS = 2

_REFUSAL = (
    "I can only answer questions about the UIUC course catalog data I have "
    "(sections, meeting times, gen-ed categories, grade distributions, and "
    "instructors). That question is outside what I can look up here."
)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class _State(TypedDict, total=False):
    question: str
    history_block: str
    mode: str

    sql: str
    first_sql: str
    attempts: int              # repairs performed so far
    sql_seen: list[str]        # normalised text of every SQL tried (cycle-breaker)
    stalled: bool              # a repair reproduced an earlier query

    last_critique: Optional[Critique]
    rows: Optional[list[dict]]
    exec_error: Optional[str]
    best_sql: Optional[str]    # first candidate that executed cleanly
    best_rows: Optional[list[dict]]

    outcome: str               # answered | refused | failed
    answer: str
    trace: list[dict]


@dataclass
class PipelineResult:
    question: str
    mode: str
    outcome: Literal["answered", "refused", "failed"]
    answer: str
    sql: Optional[str] = None
    first_sql: Optional[str] = None
    rows: Optional[list[dict]] = None
    attempts: int = 0
    exec_error: Optional[str] = None
    trace: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------
# SQL execution (read-only, SELECT-only)
# --------------------------------------------------------------------------

def _norm(sql: str) -> str:
    """Whitespace/case-insensitive form of a query, for the cycle-breaker's
    'have we already tried this exact query' check."""
    return " ".join((sql or "").lower().split())


def _intent_mode() -> str:
    """off | repair | always. Default 'repair': the LLM intent-check only
    reviews a *repaired* query, it never vetoes the generator's first one.
    See evals/FINDINGS.md. '0'/'false' -> off, '1'/'true' -> repair, for
    backwards compatibility with the old boolean flag."""
    raw = os.environ.get("SQL_PIPELINE_INTENT_CHECK", "repair").strip().lower()
    return {"0": "off", "false": "off", "no": "off", "": "off",
            "1": "repair", "true": "repair", "yes": "repair"}.get(raw, raw)


def _run_sql(sql: str, limit: int = 200) -> tuple[Optional[list[dict]], Optional[str]]:
    """(rows, error). Only a single read-only SELECT over INCLUDED_TABLES is
    allowed through (sql_guard.check_select - parsed, so a data-modifying CTE
    can't slip past a prefix check), and it runs on a read-only,
    statement-timed-out connection that uses DATABASE_URL_RO when set."""
    stripped = sql.strip().rstrip(";").strip()
    reason = sql_guard.check_select(
        stripped, "postgres" if db.is_postgres() else "sqlite", agent_mod.INCLUDED_TABLES)
    if reason:
        return None, reason
    conn = db.get_readonly_connection(sql_guard.STATEMENT_TIMEOUT_MS)
    try:
        cur = conn.execute(stripped)
        fetched = cur.fetchmany(limit)
        rows = [dict(r) for r in fetched]
        return rows, None
    except Exception as exc:  # noqa: BLE001 - surface the DB message to the repair step
        return None, str(exc).splitlines()[0][:300]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Nodes  (closures over the shared llm / catalog / dialect)
# --------------------------------------------------------------------------

def _build_graph(llm, catalog: dict, dialect: str):
    catalog_text = catalog_mod.catalog_signature(catalog)
    terms_note = catalog_mod.terms_note()

    def n_generate(state: _State) -> _State:
        gen = generate(llm, state["question"], state.get("history_block", ""), terms_note)
        trace = [{"step": "generate", "sql": gen.sql, "in_scope": gen.in_scope}]
        if not gen.in_scope:
            return {"outcome": "refused", "answer": _REFUSAL, "trace": trace,
                    "sql": "", "first_sql": "", "attempts": 0}
        return {"sql": gen.sql, "first_sql": gen.sql, "attempts": 0,
                "sql_seen": [_norm(gen.sql)], "trace": trace}

    def n_critic(state: _State) -> _State:
        # Change A: the LLM intent-check is a repair verifier by default - it
        # only runs once a repair has happened (attempts > 0), so it can never
        # veto the generator's first query. The deterministic schema check
        # always runs.
        mode = _intent_mode()
        run_intent = mode == "always" or (mode == "repair" and state.get("attempts", 0) > 0)
        crit = critique(state["sql"], state["question"], llm, catalog, dialect,
                        run_intent=run_intent, terms_note=terms_note)
        rec = {
            "step": "critic", "attempt": state.get("attempts", 0),
            "ok": crit.ok, "hallucinated_refs": crit.hallucinated_refs,
            "static": crit.static.message, "intent": crit.intent_verdict,
            "feedback": crit.feedback,
        }
        return {"last_critique": crit, "trace": state["trace"] + [rec]}

    def n_repair(state: _State) -> _State:
        crit = state.get("last_critique")
        feedback = crit.feedback if crit else (state.get("exec_error") or "unknown error")
        if state.get("exec_error"):
            feedback = f"The query failed to execute: {state['exec_error']}. " + feedback
        new_sql = repair(llm, state["question"], state["sql"], feedback,
                         catalog_text, terms_note)
        n = state.get("attempts", 0) + 1
        seen = state.get("sql_seen", [])
        # Change B: if the repair just reproduced a query we already tried,
        # more looping won't help - stop and let finalize ship the best
        # candidate so far.
        if _norm(new_sql) in seen:
            return {"attempts": n, "stalled": True,
                    "trace": state["trace"] + [
                        {"step": "repair", "attempt": n, "sql": new_sql, "stalled": True}]}
        return {
            "sql": new_sql,
            "attempts": n,
            "exec_error": None,
            "sql_seen": seen + [_norm(new_sql)],
            "trace": state["trace"] + [{"step": "repair", "attempt": n, "sql": new_sql}],
        }

    def n_execute(state: _State) -> _State:
        rows, err = _run_sql(state["sql"])
        rec = {"step": "execute", "attempt": state.get("attempts", 0),
               "error": err, "row_count": None if rows is None else len(rows)}
        out = {"trace": state["trace"] + [rec]}
        if err:
            out["exec_error"] = err
            out["rows"] = None
        else:
            out["rows"] = rows
            out["exec_error"] = None
            # Change C: remember the first query that ran clean, so an
            # exhausted or stalled repair loop can fall back to it instead of
            # shipping a later, worse attempt.
            if not state.get("best_sql"):
                out["best_sql"] = state["sql"]
                out["best_rows"] = rows
        return out

    def n_synthesize(state: _State) -> _State:
        # Change C: ship the first query that executed cleanly. In the common
        # no-repair path best_sql == sql; when the intent check drove extra
        # repairs, best_sql is the earlier, verified-runnable candidate.
        if state.get("best_sql"):
            sql, rows = state["best_sql"], state.get("best_rows")
        else:
            sql, rows = state["sql"], state.get("rows")
        answer = synthesize(llm, state["question"], sql, rows or [])
        return {"outcome": "answered", "answer": answer, "sql": sql, "rows": rows,
                "trace": state["trace"] + [{"step": "synthesize"}]}

    def n_finalize(state: _State) -> _State:
        # Reached when repairs are exhausted, a repair stalled, or execution
        # kept failing. Prefer the best query that actually ran; only return
        # an explicit failure when nothing ever executed cleanly.
        sql = state.get("best_sql") or state.get("sql")
        rows = state.get("best_rows")
        if rows is None and sql:
            # No candidate ran cleanly earlier (the loop was all critic/repair
            # with no execute). Try the current query once - it errors back to
            # the failure branch below if it's still broken.
            rows, _ = _run_sql(sql)
        if sql and rows is not None:
            answer = synthesize(llm, state["question"], sql, rows)
            return {"outcome": "answered", "answer": answer, "sql": sql, "rows": rows,
                    "trace": state["trace"] + [{"step": "finalize", "used": "best_candidate"}]}
        why = state.get("exec_error")
        crit = state.get("last_critique")
        if not why and crit:
            why = crit.feedback
        # `why` can be raw database error text - it stays in the trace (and
        # PipelineResult.exec_error) for the evals, never in the answer.
        msg = (
            "I couldn't produce a query I'm confident is correct for that "
            f"question after {state.get('attempts', 0)} repair attempt(s), so "
            "I'd rather not give a possibly-wrong answer. Try rephrasing it, "
            "or narrowing it to one subject or term."
        )
        return {"outcome": "failed", "answer": msg,
                "trace": state["trace"] + [{"step": "finalize", "used": "failure", "why": why}]}

    # -- routing --

    def after_generate(state: _State) -> str:
        if state.get("outcome") == "refused":
            return END
        return "execute" if state.get("mode") == "baseline" else "critic"

    def after_critic(state: _State) -> str:
        crit = state["last_critique"]
        # Any query that passes the deterministic schema check gets executed -
        # its result is banked as best_sql before any intent-driven repair, so
        # a later worse attempt can't overwrite it. A surviving static defect
        # (bad table/column) is repaired if attempts remain, else finalized.
        if crit.static.ok:
            return "execute"
        if state.get("attempts", 0) < MAX_REPAIRS:
            return "repair"
        return "finalize"

    def after_repair(state: _State) -> str:
        return "finalize" if state.get("stalled") else "critic"

    def after_execute(state: _State) -> str:
        if state.get("exec_error"):
            if state.get("mode") != "baseline" and state.get("attempts", 0) < MAX_REPAIRS:
                return "repair"
            return "finalize"
        # Clean run. If the intent check (repair verifier) still flagged this
        # query and a repair budget remains, try once more - but best_sql is
        # already banked, so this can only help, not regress.
        crit = state.get("last_critique")
        if (state.get("mode") != "baseline" and crit is not None
                and crit.intent_verdict == "flawed"
                and state.get("attempts", 0) < MAX_REPAIRS):
            return "repair"
        return "synthesize"

    g = StateGraph(_State)
    g.add_node("generate", n_generate)
    g.add_node("critic", n_critic)
    g.add_node("repair", n_repair)
    g.add_node("execute", n_execute)
    g.add_node("synthesize", n_synthesize)
    g.add_node("finalize", n_finalize)

    g.set_entry_point("generate")
    g.add_conditional_edges("generate", after_generate,
                            {END: END, "critic": "critic", "execute": "execute"})
    g.add_conditional_edges("critic", after_critic,
                            {"execute": "execute", "repair": "repair", "finalize": "finalize"})
    g.add_conditional_edges("repair", after_repair,
                            {"critic": "critic", "finalize": "finalize"})
    g.add_conditional_edges("execute", after_execute,
                            {"synthesize": "synthesize", "repair": "repair", "finalize": "finalize"})
    g.add_edge("synthesize", END)
    g.add_edge("finalize", END)
    return g.compile()


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

_COMPILED: dict[tuple, Any] = {}


def _catalog_and_dialect() -> tuple[dict, str]:
    """The live catalog as plain sets + the sqlglot dialect for this backend."""
    catalog = {t: set(c) for t, c in catalog_mod.get_catalog().items()}
    return catalog, ("postgres" if db.is_postgres() else "sqlite")


def _get_graph(streaming: bool = False):
    """Build (and process-cache) a compiled graph. Keyed on `streaming` (the
    LLM is captured in the graph's closures, so a streaming and a
    non-streaming graph must not share a cache slot), the live schema
    signature, and the dialect - so a schema change in the same process
    rebuilds."""
    llm, _ = agent_mod._build_llm(streaming=streaming)
    catalog, dialect = _catalog_and_dialect()
    key = (streaming, dialect, tuple(sorted((t, tuple(sorted(c))) for t, c in catalog.items())))
    if key not in _COMPILED:
        _COMPILED[key] = _build_graph(llm, catalog, dialect)
    return _COMPILED[key], catalog, dialect


def run_pipeline(question: str, mode: str = "critic", history=None) -> PipelineResult:
    """Run one question through the pipeline.

    mode="critic"   -> Generator -> Critic -> Repair loop (2 attempts max)
    mode="baseline" -> Generator -> execute -> synthesize (control arm)
    """
    q = (question or "").strip()
    if not q:
        return PipelineResult(question=q, mode=mode, outcome="failed",
                              answer="Ask a question about the course data.")
    graph, _, _ = _get_graph()
    history_block = agent_mod._format_history(history)
    init: _State = {"question": q, "mode": mode, "history_block": history_block,
                    "trace": [], "attempts": 0}
    final: _State = graph.invoke(init)
    answer = final.get("answer", "I couldn't produce an answer for that.")
    if final.get("outcome") == "answered" and final.get("sql"):
        from app.citations import sources_footer
        answer += sources_footer([final["sql"]], rag_used=False, question=q)
    return PipelineResult(
        question=q,
        mode=mode,
        outcome=final.get("outcome", "failed"),
        answer=answer,
        sql=final.get("sql") or None,
        first_sql=final.get("first_sql") or None,
        rows=final.get("rows"),
        attempts=final.get("attempts", 0),
        exec_error=final.get("exec_error"),
        trace=final.get("trace", []),
    )


def hallucinated_reference(sql: Optional[str]) -> bool:
    """True if `sql` references a table or column not in the live catalog.
    Mode-independent - the eval harness runs this on both arms' final SQL."""
    if not sql:
        return False
    catalog, dialect = _catalog_and_dialect()
    res = static_schema_check(sql, catalog, dialect)
    return bool(res.unknown_tables or res.unknown_columns)
