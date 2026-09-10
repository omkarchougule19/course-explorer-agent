# Eval results: the Critic/Repair loop

**Question:** does adding a Validator/Critic + Repair loop in front of query
execution reduce hallucinated SQL and improve answers, on this app's
text-to-SQL agent?

**Short answer:** it depends entirely on how good the generator already is.

- When the generator gets the **full schema** (every column spelled out, as
  the production agent does) and a **capable model** (Groq `gpt-oss-120b`),
  it already hallucinates column/table references **0%** of the time. The
  deterministic schema check then has nothing to catch, and the LLM
  intent-check slightly *hurts* — it second-guesses a few correct queries
  into worse ones. Net: neutral-to-slightly-negative, at the cost of one
  extra LLM call and ~900 ms.
- When the generator is handicapped — a **terse schema** (table names only,
  no column list) and a **weaker model** (`gpt-4o-mini`) — it hallucinates
  references **31%** of the time. The Critic/Repair loop cuts that to **6%**,
  takes execution success from **69% → 100%**, and lifts result-match
  accuracy **54% → 62%**. This is the regime the loop is built for.

The honest read for *this app as shipped*: the loop is insurance against a
failure mode the current setup doesn't actually have. It earns its place if
the schema prompt is trimmed for cost, a cheaper model is adopted, or the
schema grows enough that the full column list no longer fits the prompt.

---

## Method

- **Test set:** [`eval_set.jsonl`](eval_set.jsonl), 24 questions —
  13 `in_scope` (with a known-correct `gold_sql`), 5 `hallucination_bait`
  (phrased to tempt a nonexistent column), 2 `empty_data`, 4 `out_of_scope`
  (incl. a prompt-injection attempt).
- **Gold answers** are `gold_sql` executed at eval time against the same
  local SQLite snapshot the agent queried (`data/courses.db`), not frozen
  rows — the dataset drifts between syncs.
- **Arms:** `baseline` = the `sql_pipeline` Generator → execute → synthesize,
  no Critic/Repair. `critic` = the same Generator plus the full Critic →
  Repair loop (≤2 repairs, then explicit failure or, if only the intent
  check still objects, best-effort execution). Same generator, same prompts,
  same model in both arms of a comparison — the only difference is the loop.
- **Runner:** `python -m evals.run` (see [`README.md`](README.md)). Numbers
  below were re-scored with `--rescore` after the scoring rules were
  finalised; raw run artifacts are under `results/` (gitignored).

### The four metrics

| metric | definition |
|---|---|
| Execution success rate | final SQL runs without a database error (over the 13 `in_scope` questions) |
| Result-match accuracy | final SQL's rows vs. the gold query's rows. *loose* forgives extra columns and `COUNT(DISTINCT x)` vs `COUNT(*)`; *strict* is exact row-tuple equality |
| Hallucinated-reference rate | the deterministic schema checker flags a table/column in the final SQL that doesn't exist. Also reported for the first (pre-repair) query |
| Repair success rate | of the queries the Critic flagged, the fraction that ended up executing cleanly |

---

## Run 1 — production config: full schema, Groq `gpt-oss-120b`

*This is the model and prompt the live site uses.*

| metric | baseline | critic | Δ |
|---|---:|---:|---:|
| 1. Execution success rate | 100.0% | 100.0% | — |
| 2. Result-match accuracy (loose) | **92.3%** | 84.6% | **−7.7** |
| &nbsp;&nbsp;&nbsp;Result-match accuracy (strict) | 61.5% | 53.8% | −7.7 |
| 3. Hallucinated-ref rate — final SQL | **0.0%** | **0.0%** | — |
| &nbsp;&nbsp;&nbsp;Hallucinated-ref rate — first SQL | 0.0% | 0.0% | — |
| 4. Repair success rate | — | 50.0% (2 flagged) | — |
| &nbsp;&nbsp;&nbsp;Answer-OK overall | **95.8%** | 91.7% | −4.2 |
| &nbsp;&nbsp;&nbsp;Refusal rate (out-of-scope) | 100.0% | 100.0% | — |
| &nbsp;&nbsp;&nbsp;'No data' handled correctly | 100.0% | 100.0% | — |
| &nbsp;&nbsp;&nbsp;Avg latency / question | 2.7 s | 3.6 s | +0.9 s |

**Why critic is worse here.** Both regressions are the same failure: the LLM
intent-check raises a plausible-sounding but wrong objection to a *correct*
query, and the repair "fixes" it into a wrong one. The clearest case (q04,
"how many distinct subjects are in the dataset?"):

```
baseline:  SELECT COUNT(DISTINCT subject) FROM sections            -> 187  (correct)
critic:    intent-check: "missing subjects that may appear in other tables"
           repair: SELECT COUNT(DISTINCT subject) FROM (
                     SELECT subject FROM sections
                     UNION SELECT subject FROM meetings
                     UNION ... )                                    -> different number
```

`sections` is authoritative for the subject list; the other tables can only
contain a subset. The critic didn't know that, and neither did the repair.

The deterministic schema check fired zero times — `gpt-oss-120b` with the
full column list simply doesn't invent columns.

---

## Run 2 — ablation: terse schema, `gpt-4o-mini`

*Generator handed only table names (no columns) via
`SQL_PIPELINE_TERSE_SCHEMA=1`, on a weaker model — to create the hallucinations
the loop is designed to catch.*

| metric | baseline | critic | Δ |
|---|---:|---:|---:|
| 1. Execution success rate | 69.2% | **100.0%** | **+30.8** |
| 2. Result-match accuracy (loose) | 53.8% | **61.5%** | **+7.7** |
| &nbsp;&nbsp;&nbsp;Result-match accuracy (strict) | 38.5% | 46.2% | +7.7 |
| 3. Hallucinated-ref rate — final SQL | **31.2%** | **6.2%** | **−25.0** |
| &nbsp;&nbsp;&nbsp;Hallucinated-ref rate — first SQL | 31.2% | 31.2% | — (same generator) |
| 4. Repair success rate | — | **83.3%** (6 flagged, 5 fixed) | — |
| &nbsp;&nbsp;&nbsp;Answer-OK overall | 66.7% | 70.8% | +4.2 |
| &nbsp;&nbsp;&nbsp;Refusal rate (out-of-scope) | 100.0% | 100.0% | — |
| &nbsp;&nbsp;&nbsp;'No data' handled correctly | 71.4% | 71.4% | — |
| &nbsp;&nbsp;&nbsp;Avg latency / question | 1.4 s | 2.5 s | +1.1 s |

The first-query hallucination rate is identical across arms (same generator);
the loop drops the *final* rate from 31% to 6% by catching bad references and
repairing them. Execution success goes to 100% because every repaired query
either runs or is turned into an explicit failure — none reach the user as a
database error.

---

## Concrete catches (from Run 2)

Full before/after for every caught case is in
`results/20260910T135215Z__catches.md`. Three representative ones:

**q05 — "What buildings does CS 225 meet in this fall?"**
```
generator:  ... FROM meetings JOIN sections ON meetings.section_id = sections.id
                WHERE sections.term = 'fall 2026'
critic:     schema check: meetings.section_id, sections.term do not exist
repair:     ... JOIN sections ON meetings.course_number = sections.course_number
                AND meetings.semester = sections.semester AND meetings.year = sections.year
                WHERE sections.subject='CS' AND sections.course_number='225'
                AND sections.semester='fall' AND sections.year=2026
answer:     Foellinger Auditorium; Campus Instructional Facility   (correct)
```

**q12 — "Enrollment status breakdown for CS sections in fall 2026"**
```
generator:  SELECT enrollment, COUNT(*) ... GROUP BY enrollment
critic:     schema check: column 'enrollment' does not exist
repair:     SELECT enrollment_status, COUNT(*) ... GROUP BY enrollment_status
answer:     A: 498, P: 8   (correct)
```

**q09 — "Which STAT courses are QR1 gen eds?"**
```
generator:  ... WHERE gen_ed_categories.category = 'QR1'
critic:     schema check: column 'category' does not exist
repair:     ... WHERE gen_ed_categories.qr = 1
```
(Partial: `qr` is real but holds the string `'QR1'`, not `1` — the schema
check confirms a column exists, it can't check value domains, so this one
still returns empty. A caught reference, an imperfect repair.)

---

## Caveats

- **n = 24, one run per configuration, temperature 0.** Small sample, some
  LLM nondeterminism remains. Treat single-question deltas as anecdote and
  the rate-level differences as the signal.
- **"Repair success rate" is lenient** — "of flagged queries, the fraction
  that ended up executing", not "of truly-broken queries, the fraction
  fixed". The conservative intent check still flags some queries that would
  have worked.
- **`grade_distributions` and `teachers_ranked_excellent` are empty** this
  term (upstream not published). The `empty_data` questions check the agent
  says so rather than inventing data; they can't check retrieval.
- **Provider is a variable.** Run 1 is Groq, Run 2 is OpenAI. Within each
  run, baseline and critic use the same model — that comparison is clean.
  Cross-run comparison (Groq vs OpenAI) is not.
