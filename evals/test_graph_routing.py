"""
test_graph_routing.py

Routing checks for the Generator -> Critic -> Repair graph, using a scripted
fake chat model so no real LLM (or API key) is needed:

    .venv/Scripts/python -m evals.test_graph_routing

These pin the three fixes from evals/FINDINGS.md:
  A - the LLM intent-check never runs on the generator's first query (a
      static-clean first query costs one generate + one synthesize, nothing
      more).
  B - a repair that reproduces an earlier query stops the loop.
  C - when the intent-check drives extra repairs, the first query that
      executed cleanly is the one shipped, not a later worse attempt.
"""

from app.sql_pipeline import graph as G

_CATALOG = {
    "sections": {"year", "semester", "subject", "course_number", "instructor", "crn"},
    "meetings": {"year", "semester", "subject", "course_number", "crn", "building"},
}


class _Stub:
    """Fake chat model. Branches on prompt type and returns scripted text."""

    def __init__(self, gen_sql, repairs=(), intent_verdicts=()):
        self.gen_sql = gen_sql
        self.repairs = list(repairs)
        self.intents = list(intent_verdicts)
        self.calls: list[str] = []

    def invoke(self, prompt):
        p = str(prompt)
        if '"in_scope"' in p:
            kind, out = "gen", '{"in_scope": true, "sql": "%s"}' % self.gen_sql
        elif "repair step" in p:
            kind, out = "repair", (self.repairs.pop(0) if self.repairs else self.gen_sql)
        elif "SQL reviewer" in p:
            v = self.intents.pop(0) if self.intents else "ok"
            kind, out = "intent", '{"verdict": "%s", "issue": "scripted"}' % v
        elif "Answer the question below using ONLY" in p:
            kind, out = "synth", "SYNTH_OK"
        else:
            kind, out = "?", "?"
        self.calls.append(kind)
        return type("R", (), {"content": out})()


def _run(stub, mode="critic"):
    g = G._build_graph(stub, _CATALOG, "sqlite")
    return g.invoke({"question": "q", "mode": mode, "history_block": "",
                     "trace": [], "attempts": 0})


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_a_clean_query_skips_the_intent_check():
    s = _Stub("SELECT instructor FROM sections WHERE subject='CS'")
    r = _run(s)
    assert r["outcome"] == "answered", r["outcome"]
    assert s.calls == ["gen", "synth"], s.calls  # no critic/intent/repair


@case
def test_static_defect_is_caught_and_repaired():
    s = _Stub("SELECT professor FROM sections WHERE subject='CS'",
              repairs=["SELECT instructor FROM sections WHERE subject='CS'"],
              intent_verdicts=["ok"])
    r = _run(s)
    assert r["outcome"] == "answered" and "instructor" in r["sql"], r["sql"]
    assert "repair" in s.calls


@case
def test_c_ships_first_clean_candidate_not_a_worse_later_repair():
    s = _Stub("SELECT professor FROM sections WHERE subject='CS'",
              repairs=["SELECT instructor FROM sections WHERE subject='CS'",
                       "SELECT instructor, crn FROM sections WHERE subject='CS' AND year=9999"],
              intent_verdicts=["flawed", "flawed", "flawed"])
    r = _run(s)
    assert r["sql"] == "SELECT instructor FROM sections WHERE subject='CS'", r["sql"]


@case
def test_b_cycle_breaker_stops_a_repair_that_repeats_an_earlier_query():
    s = _Stub("SELECT professor FROM sections",
              repairs=["SELECT instructor FROM sections",       # static-ok
                       "SELECT professor FROM sections"],        # == generator -> stall
              intent_verdicts=["flawed", "flawed"])
    r = _run(s)
    assert r["outcome"] == "answered", r["outcome"]
    assert r["sql"] == "SELECT instructor FROM sections", r["sql"]


@case
def test_baseline_mode_is_unchanged():
    s = _Stub("SELECT instructor FROM sections")
    r = _run(s, mode="baseline")
    assert r["outcome"] == "answered" and s.calls == ["gen", "synth"], s.calls


def main() -> int:
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print()
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
