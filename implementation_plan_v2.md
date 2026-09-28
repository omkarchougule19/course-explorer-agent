# Implementation plan (v2)

What is still to do, in priority order. Written from the code as of
2026-09-24 (commit `bbf824e`). Every item has a problem, a plan and a
done-when. The *why* behind everything already built is in
[`DECISIONS_v2.md`](DECISIONS_v2.md). The original plan (v1) was removed on
2026-09-28; its full dated history is in git
(`git show bbf824e:implementation_plan.md`).

Status values: `todo`, `partly done`, `blocked` (needs something outside the
code), `owner` (needs the owner's decision).

Suggested order (revised 2026-09-28 after the `improvement-strategist`
review): 18 first (it's paid on every question and stalls the whole site),
then 19-21 (small, measurable accuracy fixes), then 1-3, then 22-24, then the
rest. Items 18-26 came from that review; older item numbers are unchanged
because other docs cite them.

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
- **Measured 2026-09-28:** a typical question is 2 LLM calls of ~2,750 input
  tokens each (~5.8K per question). Three questions in a row took 4.4 s,
  28.8 s and 44.5 s: the 8K tokens/minute limit, not the work, sets the pace
  once more than one question arrives in a minute. Prompt caching doesn't
  help (item 26), so cutting tokens per step (item 25's schema tools) or a
  higher tier are the remaining levers.
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
- **Also done (2026-09-28):** each summary's `db` label now reflects the
  database actually used, and `--rescore` honours `--db`.
- **Plan:** make `env` the default when `DATABASE_URL` is set.
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

### 18. Build the agent once, and off the event loop — `todo` (highest priority)
- **Problem:** `build_agent()` runs for every question and costs 1.5-1.8 s
  before the first LLM call (measured locally against Neon): schema reflection
  in `_CappedSQLDatabase` 1.0-1.3 s, 1-3 Groq clients 0.25-0.5 s each, and
  `_sections_empty()` opens its own connection (~170 ms). On `/ask/stream` this
  runs synchronously inside an `async` generator (`agent.py:761`, `:766`), so
  with one uvicorn worker every page request probably stalls for ~1.7 s
  whenever a question starts (derived from the code and timings, not observed
  live).
- **Plan:** build the engine, `SQLDatabase` and agent executors once per
  process, cached on (provider, model, streaming, key values), with a separate
  entry for the fallback model; drop `_sections_empty()` or fold it into the
  cached `_data_notes()` counts; as a stopgap, run `build_agent` via
  `run_in_threadpool`.
- **Check:** `test_llm_failover.py`, `test_agent_guards.py` (they swap env
  keys), a timing script (`build_agent` under 5 ms on the second call), and a
  Groq question on both paths.
- **Done when:** time to first token drops by ~1.5 s and pages stay responsive
  while a question starts.

### 19. IS and STAT fall 2026 have no meeting times — `owner` (sync running)
- **Problem:** IS has 340 fall 2026 sections and 0 meeting rows, STAT 163 and
  0 (scraped 08-02/03, before meetings were parsed). Meeting-time questions
  return nothing, and the "No 8ams" / "Done by 5" / "No Fridays" filters let
  all 503 through (a section with no timed meetings passes).
- **Plan:** the owner runs `python -m app.sync_requests --run IS STAT`
  (agreed 2026-09-28); then add a check (in `_data_notes()` or `/freshness`)
  that flags any subject-term with sections but no meetings.
- **Done when:** fall 2026 sections without a meetings row drop to a handful,
  and the check exists.

### 20. Tell the prompt that fall 2026 is partial — `todo`
- **Problem:** DATA NOTES say "the latest is fall 2026", but 107 of 191
  subjects have no fall 2026 rows, so "what does X offer this semester" gets
  "no data" with no hint that the schedule just isn't synced or that spring
  2026 exists.
- **Plan:** derive one note at startup: fall 2026 is synced for N of M
  subjects; for a subject without it, say its fall schedule isn't synced yet
  (a visitor can press Sync on Departments) and offer its latest term. Add a
  gold row. Measure with two full OpenAI runs (the noise rule).
- **Done when:** the gold row passes and answer-OK doesn't drop.

### 21. Eval gold fixes and the prerequisite recipe — `todo`
- **Problem:** q04 fails every run with a correct answer (its
  `answer_contains` is a frozen "187"; the live count is 191). q17 is labelled
  `no_data`, but prerequisites have been loaded since 09-10; relabelled, it
  fails for a real reason: answers list alternatives ("125 or 128") as all
  required. q25 failed 5 of 8 runs because the prompt's `NOT EXISTS` recipe has
  no subject filter and the model copies it.
- **Plan:** q04 → drop the frozen needle; q17 → `in_scope` with gold SQL on
  `prerequisites`; make the recipe a complete example with
  `s.subject = 'CS'`; add a rule that rows in one `group_index` are
  alternatives joined by "or". `--rescore` first (free), then
  `--ids q04,q17,q25` three times on OpenAI.
- **Done when:** q04 passes on rescore and q17/q25 pass in 3 of 3 runs.

### 22. Compress responses — `todo`
- **Problem:** nothing is gzipped: the home page is 137 KB raw vs 42.8 KB
  gzipped; `/sections?subject=CS` 44 KB and `/freshness` 45 KB of JSON.
- **Plan:** first check whether Render's edge already compresses
  (`curl -sI -H 'Accept-Encoding: gzip' <site>/style.css`); if not, add
  Starlette's `GZipMiddleware(minimum_size=1024)` (it already skips
  `text/event-stream`).
- **Done when:** pages arrive compressed and `/ask/stream` still streams.

### 23. Record tokens on the production eval arm — `done 2026-09-28`
- The prod arm now records input, output and cached tokens per LLM call
  (`usage` in each record; `total_cached_tokens` and `cache_hit_rate` in the
  summary), carried through `--rescore`.

### 24. One source for the current term — `todo`
- **Problem:** the current term is hard-coded in four places (`app/terms.py`,
  `index-page.js`, `departments-page.js`, `calendar-page.js`).
- **Plan:** return `current_term` from `/stats` and read it on the three pages.
- **Done when:** a term rollover is a one-line change in `terms.py`.

### 25. Smaller follow-ups from the review — `todo`
- Remove the unused schema tools (this is item 2.1; ~110 tokens per step).
- Move the `esc()` / `getJSON()` helpers repeated in 7 page scripts into one
  shared file (one XSS helper instead of seven; part of item 15).
- A `no_data` outcome in `ask_log` so the admin page shows how often coverage
  gaps bite.
- Eval rows for the RAG tool (never exercised by any gold question), a drop
  deadline, a follow-up with history, an unsynced subject, "what is X about"
  and "is X hard" (the last two are downvote patterns). Extends item 5.

### 26. Groq prompt caching — `closed 2026-09-28` (measured: no hits)
- Groq documents automatic caching for `openai/gpt-oss-120b`, with cached
  tokens exempt from rate limits. Measured on our free-tier key: **0 cached
  tokens in 8 calls** - 6 agent calls over 3 questions (each question's second
  call repeats the first call's ~2,750-token prefix; all three share the
  system prompt) and 2 byte-identical direct API calls, whose raw `usage` had
  no `prompt_tokens_details` at all and no faster prompt processing. The docs
  say hits "are not guaranteed" and don't mention tiers.
- **Decision:** don't plan around caching. The harness keeps recording
  `cache_hit_rate`, so a change on Groq's side would show up in the next eval.
  Keep the system prompt static anyway (it costs nothing).

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
- **Tooling:** the `startup-critic` agent (findings: item 1) and the
  `improvement-strategist` agent (findings: items 18-26).
- **2026-09-28 cleanup:** Gemini provider removed (9 packages; no Gemini key
  was deployed); `.claude/skills/` and `skills-lock.json` untracked (77% of the
  repo's tracked bytes; kept locally); CI for the offline tests declined by the
  owner.
- **Docs, 2026-09-28:** every doc rewritten from the code as a `_v2` file
  (the README renamed back to `README.md` so GitHub shows it); v1 files
  removed (in git at `bbf824e`).
