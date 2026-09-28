# Implementation plan (v2)

What is still to do, in priority order. Written from the code as of
2026-09-24 (commit `bbf824e`). Every item has a problem, a plan and a
done-when. The *why* behind everything already built is in
[`DECISIONS_v2.md`](DECISIONS_v2.md). The original plan (v1) was removed on
2026-09-28; its full dated history is in git
(`git show bbf824e:implementation_plan.md`).

Status values: `todo`, `partly done`, `blocked` (needs something outside the
code), `owner` (needs the owner's decision).

Suggested order: 1-3 first (they are what students feel: slow cold starts and
slow answers), then 4-6 (answer quality you can prove), then 7-10 (data), then
11-17 (housekeeping and UI).

---

## Open items

### 1. Cut cold-start time — `todo`
- **Problem:** time to first response is about 2.0-2.2 s on Neon against
  0.6 s on SQLite (local measurement; Render's shared CPU is slower). Almost
  all of the difference is two startup hooks in `app/api.py`:
  `embeddings.warmup()` (~0.65 s warm, ~2.2 s cold disk) and
  `_ensure_app_tables()` (a fresh Neon connection and 12 DDL statements with 4
  commits, ~0.67 s).
- **Plan:**
  1. Run `warmup()` in a background thread from the startup hook, with a lock
     in `embeddings._get_model()` so a semantic question arriving during the
     load waits instead of loading a second copy (+~100 MB).
  2. Move table creation to the Render build step (env vars are available
     there), or replace it with one `to_regclass` check that skips the DDL
     when all four tables exist.
  3. Then: pool Neon connections behind `db.get_connection()` (~170 ms per
     request today; keep `get_readonly_connection()` out of the pool), cache
     the Groq clients per (provider, model, streaming) instead of building 1-3
     per question (~124 ms per SSL context), import the scraper lazily in
     `app/sync_requests.py`, and add `python -m compileall -q app` to the
     build.
- **Check:** `evals/test_request_guards.py`, `test_agent_guards.py`,
  `test_llm_failover.py` (they swap env keys, so a client cache must key on
  them), and a "courses about X" question right after a cold start.
- **Done when:** time to first 200 on Neon is within ~0.1 s of SQLite, and
  the first semantic question after a cold start still answers.

### 2. Slow answers on Groq — `partly done`
- **Problem:** one agent step sends several thousand tokens and Groq's free
  tier allows 8,000 tokens per minute for `openai/gpt-oss-120b`, so
  multi-step questions and simultaneous users get throttled. Observed: 4-37 s
  for single questions, 31-56 s with two in flight, 59-150 s with four.
- **Done so far:** concurrency ceiling (2 on Render); 429 failover to qwen; the
  prompt rewrite (about 2,200 tokens rendered) with a suffix that no longer
  invites schema-discovery steps.
- **Plan:**
  1. Remove `sql_db_list_tables`, `sql_db_schema` and `sql_db_query_checker`
     from the agent's tools in `build_agent()` (the schema is in the prompt),
     so a normal question is one query call plus the answer.
  2. Measure tokens per step and per question on Groq for 5 typical questions
     before and after; record in `DECISIONS_v2.md` §6.
  3. If still slow, price Groq's paid Dev tier against the alternatives.
- **Done when:** three consecutive multi-step questions finish under 30 s each
  on Groq with no 429.

### 3. Confirm the assistant on the production model — `partly done`
- **Problem:** the 2026-09-24 prompt eval ran on `gpt-4o-mini` (to spare
  Groq's budget); on Groq's `openai/gpt-oss-120b` only three targeted questions
  were checked.
- **Plan:** add `--repeats N` to `evals/run.py` and report per-question pass
  rates; run the 29 questions on Groq at a quiet time over two days
  (`--limit`/`--ids` to stay inside the daily budget), and decide from the
  repeated numbers whether `SQL_PIPELINE=critic` is worth it in production.
- **Done when:** per-question pass rates over at least 3 repeats on Groq are in
  `evals/RESULTS.md`, and the provider/mode decision is recorded.

### 4. Evals default to the live database — `partly done`
- **Problem:** `evals/run.py` still defaults to `--db sqlite` (a local snapshot
  of 14,714 sections) while students use Neon (19,848).
- **Done so far:** the `%` bug that broke gold SQL on Neon is fixed in
  `app/db.py`; full runs work with `--db env`.
- **Plan:** make `env` the default when `DATABASE_URL` is set, and print the
  database used at the top of every results file.
- **Done when:** a default run uses Neon and its summary names the database.

### 5. Add the missing gold questions — `todo`
- **Problem:** the eval set has no time-of-day question (the bug fixed
  2026-09-24) and no "meets only on Tuesdays and Thursdays" question.
- **Plan:** add q30 (sections starting at or after a time, gold SQL using the
  minutes-after-midnight expression) and q31 (only-TR, once item 9 settles the
  definition). Gold SQL checked on Neon.
- **Done when:** both rows exist and pass on the current prompt.

### 6. Broken instructor links in answers — `todo`
- **Problem:** the model sometimes writes `https://instructor.html?name=...`
  instead of `/instructor.html?name=...`; `md.js` then refuses to link it.
- **Plan:** a small post-processor on the final answer that rewrites
  `](https://instructor.html` and `](https://?course=` to the site-relative
  form, with a unit test in `evals/test_agent_guards.py`.
- **Done when:** the test passes and a 10-question sample shows no malformed
  links.

### 7. Resume the fall 2026 department sync — `todo`
- **Problem:** only 84 of 191 subjects have fall 2026 data; the catch-up sync
  was stopped at the WAF wall on 2026-09-15.
- **Plan:** from the operator's machine, after a cooldown, run
  `python -m app.sync_requests --run` over the remaining subjects (subjects
  with no `year=2026 AND semester='fall'` rows) in small batches, stopping at
  the wall as designed.
- **Done when:** every subject with fall 2026 courses has rows.

### 8. Grade and ranking data are empty — `blocked` on upstream
- **Problem:** `grade_distributions` and `teachers_ranked_excellent` have 0
  rows because the upstream datasets haven't published terms inside the
  window. The assistant says so correctly; the Grade History tab just looks
  empty.
- **Plan:** check upstream each month; when data appears, run
  `load_grades.py` / `load_tre.py` locally against Neon and add gold rows.
  Meanwhile add a one-line "not published yet" note to the Grade History tab.
- **Done when:** rows exist, or the tab explains the gap.

### 9. Confirm the `P` status and the "only Tue/Thu" meaning — `blocked` on a data sample
- **Problem:** `P` is shown as "Pending" by inference; not confirmed. "Meets
  only on Tuesdays and Thursdays" isn't defined at course level (a TR lecture
  can have a Friday discussion).
- **Plan:** read one raw section XML for a `P` section against UIUC's schema;
  choose the Tue/Thu definition (every meeting vs the lecture) and write it
  into the prompt and q31.
- **Done when:** the label is confirmed or corrected and the definition is
  recorded.

### 10. Record this round of hardening in `security_findings.md` — `todo`
- **Plan:** one remediation table for 2026-09-23/24 (read-only SQL guard and
  role, request guards, strict CSP, header-only admin token, body cap, pinned
  dependencies), then a fresh `course-app-redteam` run against a local
  instance.
- **Done when:** the table and a new dated run section exist.

### 11. Sync the local environment to production — `todo`
- **Problem:** local development runs Python 3.13 with older versions of
  several packages (e.g. `langchain` 1.3.14 vs 1.4.2 in production), so local
  tests don't exercise exactly what's live.
- **Plan:** a Python 3.14 venv installed from `requirements.txt`, then rerun
  every offline test.
- **Done when:** local and production versions match and the tests pass.

### 12. Render service name and deploy settings — `owner`
- **Decide:** rename the Render service to match `render.yaml`
  (`illini-course-copilot`, which changes the public URL) or change
  `render.yaml` to `course-explorer-agent`; and whether to turn on auto-deploy
  (today every deploy is manual, which the owner may prefer).

### 13. Real name in commit history — `owner`
- Every commit is authored under the operator's real name. Fixing it means a
  history rewrite and force-push (destructive, breaks clones). Owner decides.

### 14. Panels hidden after a hard scroll jump — `partly done`
- **Problem:** with motion on, jumping past below-the-fold panels (Home/End)
  can leave them at `opacity: 0`. `motion.js` already marks anything scrolled
  past as seen on a restored position; a jump during the session isn't
  covered.
- **Plan:** a throttled passive `scroll` listener that marks any `.reveal`
  above the viewport bottom as shown, plus a 3 s fallback timer.
- **Done when:** after `scrollTo(0, scrollHeight)`, no `.reveal:not(.in)`
  remains above the fold.

### 15. One source for theme tokens and the nav — `todo`
- **Problem:** dark-mode tokens are defined in both `style.css` and
  `theme-genz.css`; the nav is copy-pasted into 7 pages.
- **Plan:** fold the final tokens into `style.css`; generate the nav from one
  partial with a tiny build script (no runtime cost), plus an offline check
  that every page's nav is identical.
- **Done when:** one place defines each, and the check passes.

### 16. Re-runnable browser checks — `todo`
- **Plan:** save the motion, phone-width (390/320 px) and CSP checks as
  `evals/ui_smoke.py` (needs Chrome and a running server; run by hand before
  UI commits). Include the check that an injected inline script is blocked.
- **Done when:** `python -m evals.ui_smoke` passes against a local server.

### 17. Housekeeping — `todo`
- Add `.gitattributes` (`* text=auto eol=lf`) to stop the CRLF warnings.
- Decide whether `qa_log.txt` stays tracked.
- A right-edge fade on the phone chip row (the swipe hint relies on a clipped
  chip).

---

## Done (see `DECISIONS_v2.md` for the reasoning)

- **Security, 2026-09-23/24:** model-written SQL parsed and allowlisted, run
  read-only with a timeout, on a SELECT-only role; request guards (reserved
  rate-limit slots, concurrency ceiling, scrubbed errors, input bounds,
  feedback caps, 256 KB body cap); strict script CSP with page scripts moved to
  files; header-only admin token; safer links; hash-pinned, audited
  dependencies. All deployed and verified in production.
- **Assistant, 2026-09-24:** `SYSTEM_CONTEXT` rewritten (answer-OK 62.1% →
  89.7% on Neon), live DATA NOTES, fixed refusal and no-data sentences, correct
  time-of-day filtering, and the `%` fix in `app/db.py`. Deployed.
- **Earlier:** hybrid SQL + RAG agent with streaming and citations; 429
  failover to qwen; opt-in Critic/Repair pipeline and eval harness; schedule
  sub-filters instead of LLM chips; chat persistence across pages; page
  transitions and no-cache headers; the assistant fixes of 2026-09-19 (text
  course numbers, prerequisites recipe, result truncation, friendly stop).
- **Tooling:** the `startup-critic` agent, whose findings are item 1.
- **Docs, 2026-09-28:** every doc rewritten from the code as a `_v2` file
  (the README renamed back to `README.md` so GitHub shows it); v1 files
  removed (in git at `bbf824e`).
