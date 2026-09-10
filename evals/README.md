# Evals: does the Critic/Repair loop reduce hallucinated SQL?

This directory measures the `app/sql_pipeline/` Generator → Critic → Repair
pipeline against a control, on a fixed question set with known-correct
answers. Methodology and the current numbers are in
[`RESULTS.md`](RESULTS.md); the short version and the headline table are also
in the project [README](../README.md#evals).

## Layout

| file | what it is |
|---|---|
| `eval_set.jsonl` | 24 questions, four buckets (see below), each with a `gold_sql` where one exists |
| `run.py` | the harness — runs the set through each arm, scores it, writes `results/` |
| `metrics.py` | scoring helpers (result matching, refusal/no-data detection) |
| `RESULTS.md` | the first run's numbers and interpretation (pre-A+B+C) |
| `FINDINGS.md` | why the loop didn't help at first, and the three routing fixes (A+B+C) |
| `test_static_check.py` | offline: the static schema checker has no false positives on valid SQL |
| `test_graph_routing.py` | offline: the Generator→Critic→Repair routing, via a scripted fake model |
| `results/` | per-run JSON + `latest_summary.md` + `*__catches.md` (gitignored) |

## The question set

| bucket | n | purpose |
|---|---|---|
| `in_scope` | 13 | real questions the agent should answer; scored on result-match vs `gold_sql` |
| `hallucination_bait` | 5 | phrased to tempt a nonexistent column ("average professor rating", "waitlist count", "prerequisites as a list") — a good answer says it has no such data |
| `empty_data` | 2 | `grade_distributions` / `teachers_ranked_excellent` are empty this term — answer must say so, not invent |
| `out_of_scope` | 4 | general knowledge, another university, a prompt-injection attempt — must refuse |

Gold answers are `gold_sql` **executed at eval time** against the same
database the agent used, not frozen rows — the dataset is a per-sync
snapshot that drifts between refreshes and differs between the local SQLite
copy and Neon.

## Arms

| arm | pipeline |
|---|---|
| `baseline` | `sql_pipeline` Generator → execute → synthesize. No Critic, no Repair. **The control.** |
| `critic` | the full Generator → Critic → Repair loop (2 repair attempts, then explicit failure) |
| `prod` | the live `create_sql_agent` (`app.agent`), SQL captured via a callback. Optional reference column — it also changes the architecture, so it is **not** the headline comparison. |

## The four metrics

1. **Execution success rate** — final SQL runs without a database error (over `in_scope`).
2. **Result-match accuracy** — final SQL's rows match the gold query's rows.
   *loose* forgives extra columns and `COUNT(DISTINCT x)` vs `COUNT(*)`;
   *strict* is exact row-tuple equality.
3. **Hallucinated-reference rate** — the static schema checker (the same one
   the Critic uses) flags a table/column in the final SQL that doesn't exist.
   Measured mode-independently on both arms.
4. **Repair success rate** — of the queries the Critic flagged, the fraction
   that ended up executing cleanly. The conservative intent check sometimes
   flags a query that would have worked, so this is *not* "fraction of
   truly-broken queries fixed".

## Running it

```bash
# baseline + critic, local SQLite snapshot, force a provider so it doesn't
# drain the shared Groq budget:
.venv/Scripts/python -m evals.run --provider openai -y

# quick iteration:
.venv/Scripts/python -m evals.run --arms critic --limit 6 --provider openai -y

# include the production create_sql_agent as a reference column:
.venv/Scripts/python -m evals.run --arms baseline,critic,prod --provider openai -y

# static-checker unit checks (no LLM, no cost):
.venv/Scripts/python -m evals.test_static_check
```

A full baseline+critic run is ~150–200 model calls. `--limit` and
`--provider` are your friends; `--db env` uses `DATABASE_URL` instead of the
local snapshot if you want to eval against Neon.
