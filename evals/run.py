"""
run.py - the eval harness. Runs evals/eval_set.jsonl through one or more
"arms" and reports the four metrics (execution success, result-match
accuracy, hallucinated-reference rate, repair success rate). Full method and
metric definitions: evals/README.md and evals/RESULTS.md.

Arms: `baseline` (Generator -> execute -> synthesize, the control), `critic`
(the full Generator -> Critic -> Repair loop), `prod` (the live
create_sql_agent, SQL captured via a callback - a reference column, not the
headline; baseline vs. critic is).

    python -m evals.run --provider openai -y          # baseline + critic
    python -m evals.run --arms critic --limit 6
    python -m evals.run --rescore 20260910T133659Z    # re-score, no LLM calls

--provider clears the other provider keys so app.agent picks the one asked
for; --limit and --sleep help with rate limits.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SET_PATH = ROOT / "evals" / "eval_set.jsonl"
RESULTS_DIR = ROOT / "evals" / "results"

ALL_ARMS = ("baseline", "critic", "prod")


# --------------------------------------------------------------------------
# Environment setup - must happen before app.* imports
# --------------------------------------------------------------------------

def _disable(key: str) -> None:
    """Neutralise an env var so it stays neutralised. Several app modules call
    load_dotenv() at import time; that repopulates anything merely popped,
    because python-dotenv (override=False) only skips keys already present.
    Setting an empty string keeps the key 'present' and falsy."""
    os.environ[key] = ""


def _prepare_env(provider: str | None, use_sqlite: bool) -> None:
    if use_sqlite:
        # Reproducible: score against the local snapshot, not live Neon.
        _disable("DATABASE_URL")
        _disable("DATABASE_URL_RO")
    if provider:
        keep = {
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
        }[provider]
        for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
            if key != keep:
                _disable(key)
    # The agent path must not recurse into the pipeline for the prod arm.
    os.environ.pop("SQL_PIPELINE", None)
    # Score SQL/answer quality, not the provenance footer.
    os.environ["ANSWER_CITATIONS"] = "0"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_set(limit: int | None) -> list[dict]:
    items = [json.loads(line) for line in SET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return items[:limit] if limit else items


# --------------------------------------------------------------------------
# Arm runners
# --------------------------------------------------------------------------

def _with_retry(fn, tries: int = 5, base: float = 2.0):
    """Retry `fn` on provider rate-limit errors with exponential backoff.
    Free-tier keys (Groq's 8k tokens/min especially) 429 constantly under a
    batch load; without this the run is mostly noise."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if attempt == tries - 1 or ("rate limit" not in msg and "429" not in msg):
                raise
            wait = base * (2 ** attempt)
            print(f"      rate-limited, retrying in {wait:.0f}s "
                  f"(attempt {attempt + 2}/{tries})")
            time.sleep(wait)


def _record(arm: str, answer: str, t0: float, **over) -> dict:
    """One raw per-question record. `over` fills in the fields an arm knows;
    the rest default to the 'nothing happened' values score() expects."""
    rec = {"arm": arm, "answer": answer, "outcome": "answered", "final_sql": None,
           "first_sql": None, "attempts": 0, "exec_error": None, "rows": None,
           "trace": [], "latency_ms": int((time.monotonic() - t0) * 1000)}
    rec.update(over)
    return rec


def run_pipeline_arm(item: dict, mode: str) -> dict:
    from app.sql_pipeline import run_pipeline
    t0 = time.monotonic()
    r = run_pipeline(item["question"], mode=mode)
    return _record(mode, r.answer, t0, outcome=r.outcome, final_sql=r.sql,
                   first_sql=r.first_sql, attempts=r.attempts,
                   exec_error=r.exec_error, rows=r.rows, trace=r.trace)


def run_prod_arm(item: dict) -> dict:
    from app.agent import build_agent, build_agent_input, friendly_error
    from app.citations import SQLCapture
    cap = SQLCapture()
    t0 = time.monotonic()
    try:
        agent = build_agent()
        out = agent.invoke({"input": build_agent_input(item["question"])},
                           config={"callbacks": [cap]})
        answer = out.get("output", str(out)) if isinstance(out, dict) else str(out)
    except Exception as exc:  # noqa: BLE001
        answer = friendly_error(exc)
    return _record(
        "prod", answer, t0,
        final_sql=cap.queries[-1] if cap.queries else None,
        first_sql=cap.queries[0] if cap.queries else None,
        trace=[{"step": "sql_db_query", "sql": q} for q in cap.queries],
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score(item: dict, rec: dict) -> dict:
    from evals import metrics
    from app.sql_pipeline.graph import hallucinated_reference, _run_sql

    expect = item.get("expect", "answer")
    gold_sql = item.get("gold_sql")
    answer = rec.get("answer") or ""

    # -- executed_ok + candidate rows for matching --
    final_sql = rec.get("final_sql")
    cand_rows = rec.get("rows")
    exec_err = rec.get("exec_error")
    if final_sql and cand_rows is None:
        cand_rows, exec_err = _run_sql(final_sql)
    if not final_sql:
        executed_ok = None
    else:
        executed_ok = exec_err is None

    # -- refusal detection --
    refused = rec.get("outcome") == "refused" or metrics.looks_like_refusal(answer)
    if rec["arm"] == "prod" and refused:
        rec["outcome"] = "refused"

    # -- result match --
    match = {"strict": None, "loose": None}
    if gold_sql:
        gold_rows, gold_err = _run_sql(gold_sql)
        if gold_err:
            match = {"strict": None, "loose": None, "gold_error": gold_err}
        else:
            match = metrics.result_match(gold_rows, cand_rows)

    # -- hallucination (mode-independent static check) --
    hallu_first = hallucinated_reference(rec.get("first_sql"))
    hallu_final = hallucinated_reference(final_sql)

    # -- answer_ok by expectation --
    if expect == "refused":
        answer_ok = refused
    elif expect == "no_data":
        # The requirement for a "no data" / hallucination-bait question is
        # that the agent does NOT fabricate: either it says plainly there's
        # no such data, or it declines. Both are acceptable; inventing a
        # column or a number is not.
        answer_ok = (metrics.looks_like_no_data(answer) or refused) and not hallu_final
    else:  # "answer"
        answer_ok = bool(match.get("loose")) and metrics.answer_mentions(
            answer, item.get("answer_contains"))

    # -- repair accounting (critic arm) --
    trace = rec.get("trace") or []
    critic_steps = [t for t in trace if t.get("step") == "critic"]
    repair_triggered = any(t.get("ok") is False for t in critic_steps)
    last_critic_ok = critic_steps[-1].get("ok") if critic_steps else None
    repair_fixed = bool(repair_triggered and last_critic_ok and executed_ok)

    first_static_issue = ""
    for t in critic_steps:
        if t.get("ok") is False:
            first_static_issue = t.get("feedback", "")
            break

    return {
        **rec,
        "id": item["id"],
        "question": item["question"],
        "category": item.get("category"),
        "expect": expect,
        "produced_sql": bool(final_sql),
        "executed_ok": executed_ok,
        "match_loose": match.get("loose"),
        "match_strict": match.get("strict"),
        "gold_error": match.get("gold_error"),
        "hallucinated_first": hallu_first,
        "hallucinated_final": hallu_final,
        "refused": refused,
        "answer_ok": bool(answer_ok),
        "repair_triggered": repair_triggered,
        "repair_fixed": repair_fixed,
        "first_static_issue": first_static_issue,
    }


def aggregate(arm: str, recs: list[dict]) -> dict:
    from evals.metrics import mean

    answerable = [r for r in recs if r["expect"] == "answer"]
    with_gold = [r for r in answerable if r.get("match_loose") is not None]
    with_sql = [r for r in recs if r["produced_sql"]]
    with_first = [r for r in recs if r.get("first_sql")]
    oos = [r for r in recs if r["expect"] == "refused"]
    nodata = [r for r in recs if r["expect"] == "no_data"]
    triggered = [r for r in recs if r["repair_triggered"]]

    by_cat: dict[str, float | None] = {}
    for cat in sorted({r["category"] for r in recs}):
        by_cat[cat] = mean([r["answer_ok"] for r in recs if r["category"] == cat])

    return {
        "arm": arm,
        "n": len(recs),
        "execution_success_rate": mean([r["executed_ok"] for r in answerable
                                        if r["executed_ok"] is not None]),
        "result_match_accuracy": mean([r["match_loose"] for r in with_gold]),
        "result_match_strict": mean([r["match_strict"] for r in with_gold]),
        "hallucinated_reference_rate_final": mean([r["hallucinated_final"] for r in with_sql]),
        "hallucinated_reference_rate_first": mean([r["hallucinated_first"] for r in with_first]),
        "repair_success_rate": (round(sum(r["repair_fixed"] for r in triggered) / len(triggered), 4)
                                if triggered else None),
        "repair_triggered_n": len(triggered),
        "answer_ok_overall": mean([r["answer_ok"] for r in recs]),
        "answer_ok_by_category": by_cat,
        "refusal_rate_out_of_scope": mean([r["refused"] for r in oos]),
        "no_data_handled": mean([r["answer_ok"] for r in nodata]),
        "avg_latency_ms": int(mean([r["latency_ms"] for r in recs]) or 0),
        "avg_repairs": mean([r["attempts"] for r in recs]),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_METRIC_ROWS = [
    ("execution_success_rate", "1. Execution success rate"),
    ("result_match_accuracy", "2. Result-match accuracy (loose)"),
    ("result_match_strict", "   Result-match accuracy (strict)"),
    ("hallucinated_reference_rate_final", "3. Hallucinated-ref rate (final SQL)"),
    ("hallucinated_reference_rate_first", "   Hallucinated-ref rate (1st SQL)"),
    ("repair_success_rate", "4. Repair success rate"),
    ("answer_ok_overall", "   Answer-OK overall"),
    ("refusal_rate_out_of_scope", "   Refusal rate (out-of-scope)"),
    ("no_data_handled", "   'No data' handled correctly"),
]


def _fmt(v) -> str:
    if v is None:
        return "   -  "
    if isinstance(v, float):
        return f"{v*100:5.1f}%"
    return str(v)


def render_table(summaries: dict[str, dict]) -> str:
    arms = list(summaries)
    w = 38
    head = "Metric".ljust(w) + "".join(a.center(10) for a in arms)
    lines = [head, "-" * len(head)]
    for key, label in _METRIC_ROWS:
        row = label.ljust(w) + "".join(_fmt(summaries[a].get(key)).center(10) for a in arms)
        lines.append(row)
    lines.append("")
    lines.append("n=%d  repairs triggered: %s" % (
        summaries[arms[0]]["n"],
        ", ".join(f"{a}={summaries[a]['repair_triggered_n']}" for a in arms),
    ))
    return "\n".join(lines)


def write_outputs(ts: str, scored: dict[str, list[dict]], summaries: dict[str, dict],
                  meta: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for arm, recs in scored.items():
        (RESULTS_DIR / f"{ts}__{arm}.json").write_text(
            json.dumps(recs, indent=2, default=str), encoding="utf-8")
    (RESULTS_DIR / f"{ts}__summary.json").write_text(
        json.dumps({"meta": meta, "arms": summaries}, indent=2, default=str), encoding="utf-8")

    table = render_table(summaries)
    md = [f"# Eval run {ts}", "",
          f"- provider: `{meta['provider']}`  db: `{meta['db']}`  arms: {', '.join(summaries)}",
          f"- eval set: {meta['n']} questions", "", "```", table, "```", ""]

    if "critic" in scored:
        catches = _catch_writeups(scored["critic"])
        if catches:
            md += ["## Hallucinations the Critic caught", ""]
            md.append(catches)
            (RESULTS_DIR / f"{ts}__catches.md").write_text(catches, encoding="utf-8")

    (RESULTS_DIR / "latest_summary.md").write_text("\n".join(md), encoding="utf-8")


def _catch_writeups(critic_recs: list[dict]) -> str:
    out = []
    for r in critic_recs:
        caught = (r["hallucinated_first"] and not r["hallucinated_final"]) or \
                 (r["repair_triggered"] and r["repair_fixed"])
        if not caught:
            continue
        out.append(
            f"### {r['id']}  —  {r['question']}\n\n"
            f"**First query (generator):**\n```sql\n{r.get('first_sql')}\n```\n\n"
            f"**Critic feedback:** {r.get('first_static_issue') or '(intent flaw)'}\n\n"
            f"**Repaired query:**\n```sql\n{r.get('final_sql')}\n```\n\n"
            f"**Final answer:** {(r.get('answer') or '')[:400]}\n"
        )
    return "\n".join(out)


_RAW_KEYS = ("arm", "answer", "outcome", "final_sql", "first_sql", "attempts",
             "exec_error", "rows", "trace", "latency_ms")


def _rescore(ts: str) -> int:
    """Recompute metrics for a completed run from its saved per-item records,
    without re-calling any model. Used after a change to score()/metrics.py so
    an expensive run doesn't have to be repeated."""
    ts = ts.replace("evals/results/", "").replace("evals\\results\\", "").replace("__summary.json", "")
    _prepare_env(None, use_sqlite=True)  # gold_sql re-execution against the snapshot
    items = {d["id"]: d for d in load_set(None)}
    arm_files = sorted(RESULTS_DIR.glob(f"{ts}__*.json"))
    arms = [f.stem.split("__")[1] for f in arm_files if not f.stem.endswith("summary")]
    if not arms:
        print(f"no saved arm files for {ts} under {RESULTS_DIR}", file=sys.stderr)
        return 2

    scored: dict[str, list[dict]] = {}
    for arm in arms:
        raw = json.loads((RESULTS_DIR / f"{ts}__{arm}.json").read_text(encoding="utf-8"))
        scored[arm] = [score(items[d["id"]], {k: d.get(k) for k in _RAW_KEYS}) for d in raw]

    summaries = {a: aggregate(a, scored[a]) for a in arms}
    meta = {"timestamp": ts, "provider": "(rescored)", "db": "sqlite (local snapshot)",
            "n": len(next(iter(scored.values())))}
    write_outputs(f"{ts}-rescored", scored, summaries, meta)
    print(render_table(summaries))
    print(f"\nwrote evals/results/{ts}-rescored__*.json")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default="baseline,critic",
                    help="comma-separated: baseline,critic,prod (default: baseline,critic)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions")
    ap.add_argument("--provider", choices=["groq", "gemini", "openai"], default=None,
                    help="force LLM provider (clears the other provider keys)")
    ap.add_argument("--db", choices=["sqlite", "env"], default="sqlite",
                    help="'sqlite' (default, reproducible) ignores DATABASE_URL; 'env' uses it")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause between questions (ease tokens-per-minute limits)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the pre-run confirmation")
    ap.add_argument("--rescore", metavar="TIMESTAMP", default=None,
                    help="re-score an existing run's saved records (no LLM calls) "
                         "after a scoring change, e.g. --rescore 20260910T133659Z")
    args = ap.parse_args()

    if args.rescore:
        return _rescore(args.rescore)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ALL_ARMS]
    if bad:
        print(f"unknown arm(s): {bad}; valid: {ALL_ARMS}", file=sys.stderr)
        return 2

    _prepare_env(args.provider, use_sqlite=(args.db == "sqlite"))

    from app import db as _db  # after env prep
    items = load_set(args.limit)
    est_per_arm = {"baseline": 2, "critic": 4, "prod": 5}
    est = sum(est_per_arm.get(a, 3) for a in arms) * len(items)
    provider_label = args.provider or "auto (from env keys)"
    db_label = "postgres/env" if _db.is_postgres() else "sqlite (local snapshot)"

    print(f"Arms:      {', '.join(arms)}")
    print(f"Questions: {len(items)}")
    print(f"Provider:  {provider_label}")
    print(f"Database:  {db_label}")
    print(f"Estimated LLM calls: ~{est}")
    if not args.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted.")
                return 0
        except EOFError:
            print("\nnon-interactive; pass -y to run. aborted.")
            return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scored: dict[str, list[dict]] = {a: [] for a in arms}

    for i, item in enumerate(items, 1):
        if args.sleep and i > 1:
            time.sleep(args.sleep)
        print(f"[{i}/{len(items)}] {item['id']}: {item['question'][:70]}")
        for arm in arms:
            try:
                rec = _with_retry(
                    lambda: run_prod_arm(item) if arm == "prod"
                    else run_pipeline_arm(item, arm)
                )
                s = score(item, rec)
            except Exception as exc:  # noqa: BLE001 - one bad question shouldn't kill the run
                print(f"    {arm}: ERRORED {exc!r}")
                s = score(item, {"arm": arm, "answer": f"[harness error] {exc}",
                                 "outcome": "failed", "exec_error": str(exc),
                                 "trace": [], "latency_ms": 0})
            scored[arm].append(s)
            flag = "ok " if s["answer_ok"] else "MISS"
            extra = f" repair={s['attempts']}" if s.get("attempts") else ""
            print(f"    {arm:8s} {flag} outcome={s['outcome']} "
                  f"match={s['match_loose']} hallu_final={s['hallucinated_final']}{extra}")

    summaries = {a: aggregate(a, scored[a]) for a in arms}
    meta = {"timestamp": ts, "provider": provider_label, "db": db_label, "n": len(items)}
    write_outputs(ts, scored, summaries, meta)

    print("\n" + render_table(summaries))
    print(f"\nwrote evals/results/{ts}__*.json and evals/results/latest_summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
