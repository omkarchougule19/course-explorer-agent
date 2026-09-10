# Why the Critic/Repair loop didn't improve accuracy — and what changed

## Expectation vs. result

The loop was expected to raise answer accuracy. The first eval runs
(`RESULTS.md`) showed the opposite on the production configuration:

| full schema, `gpt-oss-120b` | baseline | critic |
|---|---:|---:|
| result-match accuracy (loose) | 92.3% | **84.6%** |
| answer-OK overall | 95.8% | **91.7%** |
| hallucinated-reference rate | 0.0% | 0.0% |

A run on `gpt-4o-mini` with the full schema was worse still (85% → 69%
result-match, 7 needless repairs). Only the handicapped **terse-schema**
ablation showed the loop helping (hallucinated-ref 31% → 6%, execution
success 69% → 100%).

## What the traces show

Every case where `critic` diverged from `baseline` was pulled and read. The
divergences split cleanly:

| what triggered the repair | count (terse run) | effect |
|---|---:|---|
| the **deterministic schema check** (a real hallucinated table/column) | 5 | 2 clean fixes, 3 partial — all net-neutral-or-better |
| the **LLM intent-check alone** (schema check had passed) | 2 | both made a correct/─ query worse or no better |

On the full-schema runs the schema check fires **zero** times — a capable
model with every column listed doesn't invent columns — so *every* repair is
intent-check-driven, and every one is a regression or a wash.

### The intent-check's five failure modes

1. **Flaw-finding bias.** Asked "is this query flawed?", the reviewer almost
   always manufactures an objection. It's the same reflex that makes a model
   agree with a user, pointed at review. `"Be conservative"` in the prompt
   didn't counteract it.

2. **Invents requirements the question never stated.**
   - q04 *"How many distinct subjects are in the dataset?"* →
     *"missing subjects that may appear in other tables"* → repair turned a
     correct `COUNT(DISTINCT subject) FROM sections` into a wrong four-table
     `UNION`.
   - q10 *"Is CS 225 listed as a gen ed?"* → *"does not specify the required
     term or year"* — but `gen_ed_categories` is a point-in-time snapshot
     (the schema notes say so). Repair bolted on bogus
     `snapshot_term = 'fall'` filters.

3. **Contradicts its own previous verdict.** Each pass is stateless and
   always objects to whatever value it currently sees:
   ```
   critic#0: "says 'spring', should be 'fall 2026'"   -> repair flips to fall
   critic#1: "says 'fall', should be 'spring 2026'"   -> repair flips to spring
   critic#2: "says 'spring', should be 'fall 2026'"   -> exhausted
   ```
   (q20, q14, q19 all did exactly this.)

4. **Ignores schema semantics.** Read `w` as "waitlist" (it is withdrawals),
   treated the gen-ed snapshot as term-scoped. The rules are in the prompt;
   the reviewer doesn't weight them.

5. **Asymmetric cost.** A false "flawed" is free for the reviewer but
   triggers a repair that usually degrades an already-correct query. Nothing
   rewards "accept".

**Conclusion:** the deterministic schema check is the entire value of the
loop. The LLM intent-check, used as a *first-pass gate*, subtracts value.

## The change: A + B + C

Implemented in `app/sql_pipeline/graph.py` (routing) — no new components.
Pinned by `evals/test_graph_routing.py` (scripted fake model, no API key).

### A — the intent-check is a repair *verifier*, not a first-pass gate

`SQL_PIPELINE_INTENT_CHECK` now takes `off | repair | always`, default
**`repair`**: the LLM intent-check runs only once a repair has happened
(`attempts > 0`). It never vetoes the generator's first query. The
deterministic schema check still runs on every pass.

Consequence: when the generator's query passes the schema check (the normal
case on the full schema), the pipeline goes straight
generate → execute → synthesize — **identical to baseline, and one LLM call
cheaper than before** (no intent call).

### B — cycle-breaker

If a repair reproduces a query already tried (whitespace/case-insensitive),
the loop stops instead of ping-ponging to the 2-repair cap.

### C — keep the first query that ran

The first repair candidate that executes cleanly is banked as `best_sql`.
If later intent-driven repairs make things worse, the banked query is what
gets synthesized — a late bad repair can't overwrite an early good one.

## Predicted impact (from the saved traces; needs a fresh run to confirm)

| | full schema, `gpt-oss-120b` | terse schema, `gpt-4o-mini` |
|---|---|---|
| result-match, baseline → critic | 92.3% → **92.3%** (was 84.6%) | ~54% → **~62%** (unchanged) |
| hallucinated-reference rate | 0% → 0% | 31% → **~6%** (unchanged) |
| answer-OK | 95.8% → **95.8%** (was 91.7%) | — |
| extra LLM calls vs. baseline, clean query | **0** (was 1) | 0 |

Net shape: **a win where the generator actually hallucinates, a no-op
(never a regression) where it doesn't.**

## How to confirm

Groq's daily token budget was spent during the investigation; it resets at
~00:00 UTC. After that:

```
.venv/Scripts/python -m evals.run --provider groq --db sqlite --sleep 25 -y
SQL_PIPELINE_TERSE_SCHEMA=1 .venv/Scripts/python -m evals.run --provider groq --sleep 25 -y
```

Compare against the committed tables in `RESULTS.md` (which are pre-A+B+C).
`evals/test_graph_routing.py` and `evals/test_static_check.py` run offline
and gate the routing/checker logic in the meantime.
