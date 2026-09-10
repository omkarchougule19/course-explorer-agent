"""
graph.py

LangGraph wiring of the Generator -> Critic -> Repair pipeline.

    generate --out of scope--> (refuse) --> END
    generate --> critic
    critic   --ok--> execute
    critic   --flaw, repairs left--> repair --> critic
    critic   --flaw, repairs exhausted--> fail
    execute  --rows--> synthesize --> END
    execute  --db error, repairs left--> repair --> critic
    execute  --db error, repairs exhausted--> fail
    fail --> END   (explicit failure string, never a silently-wrong answer)

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
from app import db
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

    last_critique: Optional[Critique]
    rows: Optional[list[dict]]
    exec_error: Optional[str]

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

def _run_sql(sql: str, limit: int = 200) -> tuple[Optional[list[dict]], Optional[str]]:
    """(rows, error). Only a single SELECT is allowed through - the Generator
    is prompted for that, and anything else is refused here rather than
    executed."""
    stripped = sql.strip().rstrip(";").strip()
    low = stripped.lower()
    if not low.startswith(("select", "with")):
        return None, "only SELECT queries are allowed"
    if ";" in stripped:
        return None, "multiple statements are not allowed"
    conn = db.get_connection()
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
        return {"sql": gen.sql, "first_sql": gen.sql, "attempts": 0, "trace": trace}

    def n_critic(state: _State) -> _State:
        run_intent = os.environ.get("SQL_PIPELINE_INTENT_CHECK", "1") not in ("0", "false", "")
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
        return {
            "sql": new_sql,
            "attempts": n,
            "exec_error": None,
            "trace": state["trace"] + [{"step": "repair", "attempt": n, "sql": new_sql}],
        }

    def n_execute(state: _State) -> _State:
        rows, err = _run_sql(state["sql"])
        rec = {"step": "execute", "attempt": state.get("attempts", 0),
               "error": err, "row_count": None if rows is None else len(rows)}
        if err:
            return {"exec_error": err, "rows": None, "trace": state["trace"] + [rec]}
        return {"rows": rows, "exec_error": None, "trace": state["trace"] + [rec]}

    def n_synthesize(state: _State) -> _State:
        answer = synthesize(llm, state["question"], state["sql"], state.get("rows") or [])
        return {"outcome": "answered", "answer": answer,
                "trace": state["trace"] + [{"step": "synthesize"}]}

    def n_fail(state: _State) -> _State:
        why = state.get("exec_error")
        crit = state.get("last_critique")
        if not why and crit:
            why = crit.feedback
        msg = (
            "I couldn't produce a query I'm confident is correct for that "
            f"question after {state.get('attempts', 0)} repair attempt(s), so "
            "I'd rather not give a possibly-wrong answer. "
        )
        if why:
            msg += f"The last problem was: {why}"
        return {"outcome": "failed", "answer": msg.strip(),
                "trace": state["trace"] + [{"step": "fail", "why": why}]}

    # -- routing --

    def after_generate(state: _State) -> str:
        if state.get("outcome") == "refused":
            return END
        return "execute" if state.get("mode") == "baseline" else "critic"

    def after_critic(state: _State) -> str:
        crit = state["last_critique"]
        if crit.ok:
            return "execute"
        if state.get("attempts", 0) < MAX_REPAIRS:
            return "repair"
        # Repairs exhausted. Hard-fail only on a real defect - a bad
        # table/column reference or a query that won't parse. If the static
        # check passes and the only remaining objection is the conservative
        # intent check, run it and answer rather than refuse a query that is
        # probably fine (the alternative over-refuses correct queries the
        # intent reviewer merely nitpicked).
        return "execute" if crit.static.ok else "fail"

    def after_execute(state: _State) -> str:
        if not state.get("exec_error"):
            return "synthesize"
        if state.get("mode") != "baseline" and state.get("attempts", 0) < MAX_REPAIRS:
            return "repair"
        return "fail"

    g = StateGraph(_State)
    g.add_node("generate", n_generate)
    g.add_node("critic", n_critic)
    g.add_node("repair", n_repair)
    g.add_node("execute", n_execute)
    g.add_node("synthesize", n_synthesize)
    g.add_node("fail", n_fail)

    g.set_entry_point("generate")
    g.add_conditional_edges("generate", after_generate,
                            {END: END, "critic": "critic", "execute": "execute"})
    g.add_conditional_edges("critic", after_critic,
                            {"execute": "execute", "repair": "repair", "fail": "fail"})
    g.add_edge("repair", "critic")
    g.add_conditional_edges("execute", after_execute,
                            {"synthesize": "synthesize", "repair": "repair", "fail": "fail"})
    g.add_edge("synthesize", END)
    g.add_edge("fail", END)
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
    """Build (and process-cache) a compiled graph. Keyed on the live schema
    signature and dialect so a schema change in the same process rebuilds."""
    llm, _ = agent_mod._build_llm(streaming=streaming)
    catalog, dialect = _catalog_and_dialect()
    key = (dialect, tuple(sorted((t, tuple(sorted(c))) for t, c in catalog.items())))
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
    return PipelineResult(
        question=q,
        mode=mode,
        outcome=final.get("outcome", "failed"),
        answer=final.get("answer", "I couldn't produce an answer for that."),
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
