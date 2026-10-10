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

### 18. Build the agent once, and off the event loop — `done 2026-09-28`
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
- **Done 2026-09-28.** `app/agent.py` caches the read-only engine and
  reflected `SQLDatabase` (per database URL), the LLM clients and the agent
  executors (per streaming/model/env settings, built under a lock);
  `_sections_empty()` stops querying once it has seen rows; the streaming path
  builds via `asyncio.to_thread`; `/ask/stream` imports `app.agent` in a worker
  thread; a startup thread pre-builds the streaming agent (Postgres only).
  Measured against Neon: `build_agent` 3.7 s / 1.7 s / 1.6 s per question →
  2.9 s once, then 0 ms; the event loop froze 1.43 s per question → 17-20 ms.
  End to end on a fresh local server: first question's first token 5.5 s →
  1.6 s, the slowest concurrent `GET /api` 871 ms → 82 ms. Six new checks in
  `evals/test_agent_guards.py`.

### 19. IS and STAT fall 2026 have no meeting times — `done 2026-09-28`
- **Problem:** IS has 340 fall 2026 sections and 0 meeting rows, STAT 163 and
  0 (scraped 08-02/03, before meetings were parsed). Meeting-time questions
  return nothing, and the "No 8ams" / "Done by 5" / "No Fridays" filters let
  all 503 through (a section with no timed meetings passes).
- **Plan:** the owner runs `python -m app.sync_requests --run IS STAT`
  (agreed 2026-09-28); then add a check (in `_data_notes()` or `/freshness`)
  that flags any subject-term with sections but no meetings.
- **Done when:** fall 2026 sections without a meetings row drop to a handful,
  and the check exists.
- **2026-09-28:** re-synced (`--run IS STAT`, no firewall wall hit). IS now
  has 335 meeting rows for 342 sections, STAT 167 for 164; fall 2026 sections
  without any meeting row dropped from ~503 to 14. On the live site, STAT's
  "No 8ams" filter now drops exactly the 2 STAT 107 sections at 8:00 AM
  (before: it passed all 163).
- **Done 2026-09-28:** `_data_notes()` now counts, per subject-term, sections
  and sections with a meeting row, and lists every subject-term with 5+
  sections and none (none today), telling the model to say meeting times
  aren't loaded rather than "none".

### 20. Tell the prompt that fall 2026 is partial — `done 2026-09-28`
- **Problem:** DATA NOTES say "the latest is fall 2026", but 107 of 191
  subjects have no fall 2026 rows, so "what does X offer this semester" gets
  "no data" with no hint that the schedule just isn't synced or that spring
  2026 exists.
- **Plan:** derive one note at startup: fall 2026 is synced for N of M
  subjects; for a subject without it, say its fall schedule isn't synced yet
  (a visitor can press Sync on Departments) and offer its latest term. Add a
  gold row. Measure with two full OpenAI runs (the noise rule).
- **Done when:** the gold row passes and answer-OK doesn't drop.
- **Done 2026-09-28.** The note is derived from the same per-subject-term
  counts (today: "fall 2026 is synced for only 84 of 191 subjects") and
  applies only to questions about that term: the first wording was
  unconditional and made the model add a fall 2026 filter to term-less
  questions (q28, ENGL gen-eds, answered "none" in 2 of 2 runs). New gold row
  q30 ("Which ECE courses are offered this fall?", `no_data`, must mention
  "synced"); the harness now also checks `answer_contains` on `no_data` rows.
  q30: 0/2 old prompt, 4/4 new.

### 21. Eval gold fixes and the prerequisite recipe — `done 2026-09-28`
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
- **2026-09-28:** q04 and q17 gold fixed (rescore of the 09-24 run: 89.7% →
  93.1%). Prompt: alternatives in one group must be joined by "or"; the
  columns' direction is spelled out (subject/course_number has the
  requirement, req_* is required) after the longer block made the model filter
  req_* for "prerequisites of CS 233" in 3 of 4 runs; the no-prerequisites
  recipe is a full example with a subject and level filter. q17 now passes
  2/2 with "CS 125 or CS 128". **Still open: q25** fails every run, old and
  new: the model drops a named filter (also seen on q28 dropping ENGL) even
  though HOW TO QUERY already says to keep every named filter.
- **Measured** (30 questions, Neon, `gpt-4o-mini`, sequential runs): old
  prompt 93.3% / 90.0%, new 90.0% / 90.0% - flat within noise, with q17 and
  q30 fixed and q13 (read terms from `academic_calendar`) and q25/q28
  (dropped filter) as the misses. Parallel runs are invalid: OpenAI rate
  limits turned 8-12 answers per run into error messages, which the prod arm
  scored as wrong; it now re-raises rate-limit errors so the harness retries.
- **Closed later that day:** q25 was a scoring artifact - the model kept the
  CS filter but answered from two queries (100-level, 200-level) and only the
  last was scored; the prod arm now falls back to the combined rows. q17's
  last flat list came from SQL without `group_index`; the prompt now requires
  it. Both pass in the final runs (item 27).

### 27. Answer questions about a named course, not the nearest lookup — `done 2026-09-28`
- **Problem:** "Why I should or should not take cs444 with saurabh gupta"
  got a table of two CRNs (14.8 s) and never the why; another run titled
  CS 444 "Computer Architecture". Cause: the who-teaches clause of the
  courses-vs-sections rule, plus no single place to get a course's facts.
  43% of logged questions name a course.
- **Plan (after a critique of a 4-layer design):** a `course_facts` tool
  (fixed SQL, both databases), one named-course answer rule replacing the
  "what is X about" rule, "a named instructor or term is a filter, not the
  topic", five `advice` eval rows with `answer_must_not`, full answers in
  `ask_log` (4,000 chars, was 500), and `md.js` external links limited to
  illinois.edu.
- **Done when:** the advice rows pass in 3 of 3 runs and answer-OK on the
  rest doesn't drop.
- **Done 2026-09-28.** 19 offline checks in `evals/test_course_facts.py`.
  Final prompt, 35 questions on Neon: 100% / 97.1% (previous prompt on 30:
  93.3% / 90.0%); q31-q34 pass in every run of the final prompt, q35 (drop
  deadline) in every run since the calendar rule requires `event_date`.
  Cost: ~15% more input tokens per question (~5.6K → ~6.6K; about 30
  questions a day on Groq's 200K instead of ~35); latency unchanged except
  advice questions (~4.7 s, two tools). Not yet confirmed on Groq's
  production model.

### 28. Calendar rows with a wrong date, and a thinner Neon calendar — `todo`
- **Problem:** on Neon, "September 4 – "10th day" add/drop deadline" is
  stored with `event_date` 2026-08-28 (the date in the title wasn't used).
  Neon has no single-course drop deadline for fall 2026 (only "withdraw from
  the semester (drop all courses)", Oct 16), while the local SQLite calendar
  has a "Drop deadline without W grade" row: the two databases were loaded
  from different calendar snapshots (79 vs 15 rows).
- **Plan:** in `app/load_calendar.py`, prefer a date written at the start of
  the title over the row's column date; reload fall 2026 on Neon; add a
  `test_calendar` case.
- **Done when:** the add/drop row reads 2026-09-04 and a "last day to drop a
  course" question gets the right date.

### 29. Chat UI and answer fixes of 2026-09-28/29 — `done 2026-09-29, deploy pending`
- **Done (committed as work in progress, not deployed):**
  - Claude/Gemini-style chat block: messages above, composer pinned at the
    bottom, centred empty state, question bubbles, auto-growing input
    (Enter sends, Shift+Enter new line); pinned to the newest line with a
    "Jump to latest" button; Trending label removed. Tested locally in
    Chrome at desktop width, dark and light.
  - `course_content_search` takes `subjects` and returns real titles (q36).
  - `LLM_MAX_TOKENS` output cap (4,000) after a 2.1M-space runaway answer.
  - `course_facts(course, completed=...)` met/missing prerequisite check,
    with 7 new offline checks (all pass).
- **Resumed 2026-09-29:**
  1. Full runs: q34 now says "You cannot take CS 444 yet" in every run (the
     check line leads with "NOT yet eligible"); q36 once hit the 4,000-token
     cap padding a table, fixed by bullets for topic-search results,
     `collapse_padding` on every answer and a runaway stop in the stream.
     Final: 100% / 97.2% (36 questions, `gpt-4o-mini`).
  2. Phone width (390 px, headless Chrome over CDP): chip row clipped by
     centring, a stray textarea scrollbar and a tall budget note - all fixed
     and re-checked.
  3. Groq `gpt-oss-120b`, q31-q36: text checks now fold Unicode spaces
     (gpt-oss writes "CS\u202f440"); q35 skipped the calendar, so
     `course_facts` now lists the drop/withdraw deadlines; q31 and q35 pass.
- **Still to do:** deploy `be1ac55`, `23c3264`, `c5c0d2d`, `ab62540` and this
  work (needs the owner's Render sign-in), then verify in production.
- **Open, not planned yet:** gpt-oss ends some judgement answers with a
  conditional "bottom line" verdict (q31).

### 30. Credit facts as columns — `done 2026-09-29, deployed (af0b2c7)`
- **Problem:** "cs courses with 3 credits eligible for graduate students"
  got "no data": the model compared the credit_hours text to 3 and counted
  only 500-level as graduate.
- **Done:** `app/credits.py` (22 offline checks in `evals/test_credits.py`),
  six columns on `sections` written by `save_sections`, `backfill_credits.py`
  run on SQLite and Neon (the read-only role reads the new columns),
  `course_facts` shows graduate credit and restrictions, two prompt lines,
  eval q37. Runs: 100% / 100% on 37 questions (`gpt-4o-mini`), q37 passes on
  Groq; after the final wording of the graduate-credit lines, one more full
  run: 100%.
- **Known gap:** the model filters on credit_min/max rather than
  grad_min/max even with a worked example, so the 7 CS courses a graduate
  student can only take for 4 hours (415, 417, 433, 437, 444, 462, 470) are
  still listed. The eval's loose match doesn't catch it.

### 31. Revise the golden set from student-style questions — `done 2026-09-29, deployed (5adf061)`
- **Input:** `evals/candidates/student_questions.jsonl` - 60 questions from
  the `student-question-writer` agent (7 personas, 9 types).
- **Done:** 26 gold rows q38-q63 (typos, vague references, schedule clashes,
  unsynced terms, a made-up course, social-engineering asks), facts checked
  on Neon; the set is 63 rows. Gold corrections found by the runs: q45 (CS
  421 does meet TR 3:30), q48 (refusing to edit the Schedule is acceptable),
  q37 (the question names no term, so gold covers every term), q40 (section
  names, not CRNs).
- **What the new rows exposed, and the fixes:**
  - Time-of-day questions: the model kept unbalancing the copied
    CAST(substr(...)) formula (q40 hit the iteration cap). Now
    `meetings.start_min/end_min` (`app/timefields.py`, backfilled on SQLite
    and Neon, 20,978 meetings); the prompt says filter on them but show
    start_time/end_time (one run had converted 930 to "11:30 AM" and
    invented a clash).
  - A per-answer repeat-query guard (`new_query_log()`).
  - Prompt: calendar ranges (event_end_date); one line per prerequisite
    group; "easy A"/GPA/grade history give the no-data sentence while grades
    are empty; vague references ("the ai class") name the candidates,
    searching spelled-out titles; meeting_type names ("discussion" =
    'Discussion/Recitation'); DATA NOTES give today's date and the next
    unsynced term per season ("next spring" = spring 2027); the
    partial-term note checks the whole subject.
  - Scoring: an in-scope answer also passes when its text holds every gold
    value.
- **Measured** (`gpt-4o-mini`, Neon): first runs 87.3% / 87.3%; final
  98.4% / 98.4% (62/63; the miss is q49, "the ai class"). Prompt about
  3,100 tokens (was ~2,600 before items 29-31). Groq `gpt-oss-120b` on 6
  new rows: 5/6.
- **Open items closed 2026-09-29:**
  - q55 (ECON unsynced, iteration cap on Groq): an empty result for a
    subject with no rows in the latest term now carries a note naming the
    subject's latest synced term (`_unsynced_note`). Groq: answered in 47 s.
  - q53 ("next spring" refused on Groq): SCOPE says terms not in the data
    are in scope. Groq: "no data for spring 2027".
  - q49 ("the ai class" without CS 440): the topic search also matches
    course titles (`_title_matches`), ranked first. Groq: "The AI class is
    CS 440".
  - The remaining candidates: 30 more gold rows (q64-q93); the set is 93.
    Not turned into rows: s017 and s019 (200+ / 36 valid answers, not
    gradeable automatically), s020 (needs another department's clash data),
    s037 (8-week drop deadline; blocked on plan item 28).
  - Also: "is it too late to add a class" was refused as a request to act;
    SCOPE now names deadline questions. Scoring: advice rows no longer fail
    on refusal wording (good answers decline one part); q40 scored on CRNs
    in the text; q73 (personal advising) expects a refusal.
- **Measured:** 93 rows, `gpt-4o-mini`: 93.5% / 93.5% before the last
  fixes, 96.8% on the final prompt (90/93). Groq `gpt-oss-120b`: q49, q53,
  q55, q81 all pass, but slowly (47-74 s: the 8K tokens/min limit).
- **Production check after deploying 5adf061:** "the ai class who teaches
  it" answered "CS 440 - Artificial Intelligence" with its fall 2026
  instructors (41 s on Groq).
- **Still open:** q74 (PSYC unsynced for fall: says so but doesn't offer
  the spring 2026 sections), q91 ("that intro programming one" picked
  CS 400 over CS 124), q45 once joined meetings without the course keys
  (noise; passed in every other run).
- **Production check after deploy:** "Which MATH 241 discussion sections are
  in the afternoon?" (the question that used to hit the iteration cap)
  answered with one query in 22.5 s, clock times shown, no minutes leaked.

### 32. Query-gap classes from the critic — `done 2026-10-03, deployed (412a920)`
- **Problem:** the `query-gap-critic` agent found 11 classes of failure with
  evidence: two-step questions (instructor -> similar course), instructor
  name formats, sections/meetings joins without crn (118/180 in traces),
  dropped filters, "similar to" crossing departments, departments named in
  words, follow-up refinements (16% of logged questions), 'Online...'
  variants, abbreviated titles, semester without year, eval traces missing
  non-SQL tools; plus prompt conflicts (one-query vs two-step, empty means
  stop, instructor rules in six places, a wrong example).
- **Done (general, per class):** data columns (instructor_last/initial,
  title_search, is_online, subjects table); topic-search arguments
  (like_course, level, taught_by, department words); deterministic checks in
  the SQL wrapper (crn-join rejection; dropped-filter, semester, no-term and
  initial-match notes; on empty results a labelled latest-term rerun, the
  real matching names, name-format, title-word and subjects hints); history
  keeps 1,200 characters of the latest answer; prompt cleanup (~210 tokens
  trimmed after the additions). 18 eval rows q94-q111, several wordings per
  class; 34 new offline checks (`evals/test_search_fields.py`).
- **Measured:** 111 rows, gpt-4o-mini on Neon: before the round-2/3 fixes 90.1% / 91.0% (q94-q111 12/18); after them 97.3% / 96.4% with q94-q111 18/18 in both runs (q01-q37 36/37). Input tokens about 9.0K per question (7.3K before items 29-32). A fresh probe of 16 unseen questions: about 11 right first time; its misses led to three more changes - instructor_last/initial literals normalized before execution, comparisons on start_time/end_time text rejected, and sql_db_query_checker removed from the toolkit (the model called it before most queries, spending steps and an LLM call each). Final (2026-10-03): 98.2% / 97.3% on 111 rows, q94-q111 18/18 in both, about 1.1 tool calls and 8.5K input tokens per question (from ~9.0K), none at the step cap; the probe that had hit the cap ("STAT classes this fall that end before 11 am") answers in two queries; Groq gpt-oss-120b 4/5 on q94/q96/q100/q103/q109 (q94 omitted the starting course while keeping the name caveat; q100 took 200 s on the 8K tokens/minute limit).
- **Production check after deploying 412a920:** "courses taught by O'Brien"
  listed all six O'Brien courses in 15 s (it used to break on the quote).
  "what courses are offered by gies" failed after 145 s: Groq's 8K
  tokens-per-minute limit - a broad question makes two calls (~4.5K, then
  ~6.5K with a large result) inside a minute; the 429 failed over to qwen,
  whose ~1K output tokens/minute also failed. Daily budgets were fine
  (986/996 requests left). Not new in item 32, but the bigger prompt makes
  it likelier. Options: on a per-minute 429, wait for the reset and retry
  the same model before failing over; a smaller result cap on Groq; fewer
  prompt tokens.
- **Still open:** follow-up refinements are fragile on unseen wordings ("and
  which of those are 3 credits?" lost the earlier level; "who teaches the
  second one?" picked the third item). Some instructor links come out as
  "?/instructor.html..." (shown as plain text by md.js, so harmless). "intro psychology sections" in a term where PSYC isn't
  synced still matches other departments' psychology titles; input tokens
  per question are higher than before items 29-32 (see Measured).

### 33. Groq's per-minute limit failed broad questions — `done 2026-10-03, deployed (94db7fb)`
- **Problem:** "what courses are offered by gies" failed in production after
  145 s. Each agent call costs ~4.3K input tokens against 8K tokens/minute, so
  multi-call questions must wait; the SDK gave up after 2 retries and the
  question failed over to qwen, which has the same per-minute limit (and ~1K
  output tokens/minute) and failed too. Daily budgets were fine.
- **Done:** `GROQ_MAX_RETRIES` (default 6) on the Groq clients, each retry
  honouring retry-after; failover to the backup only when the 429 names a
  daily limit. Offline checks in `evals/test_llm_failover.py` (daily fails
  over, per-minute doesn't, retries configurable). Locally on Groq the Gies
  question now answers in ~70 s (waits of 10 s and 52 s).
- **Production check after deploying 94db7fb:** "what courses are offered by
  gies" answered correctly (6 departments, 268 courses) in 111 s with no
  failover; before, it errored after 145 s. The answer said "the subjects
  table" - an internal name the no-internals rule should have kept out.
- **Next lever (not done):** fewer tokens per call - the prompt is ~3.45K
  tokens and tool descriptions ~0.8K, re-sent on every call; and a status
  line while waiting ("Busy, waiting for capacity...") so a 60 s pause
  doesn't look frozen.

### 34. Fewer tokens per call — `tried and reverted 2026-10-03`
- **Tried:** drop the schema tools and the default sql_db_query text, shorter
  tool descriptions, empty tables' schema left out of the prompt: fixed
  overhead per call 4,394 -> ~3,680 tokens (input per question ~8.5K ->
  ~7.25K, -15%).
- **Measured cost:** answer-OK 98.2% / 97.3% before; 91.9% / 93.7% with the
  cut, 95.5% / 94.6% with the usage guidance back in the prompt, 95.5% /
  95.5% with the topic-search description's "when to use" phrase restored -
  a steady ~2.5-point loss. The owner chose accuracy: reverted.
- **Kept from it:** a [Check] note for title/description searches on a 1-3
  letter fragment ('%ai%' matched "Tech and Advertising Campaigns").
- **Lessons:** tool descriptions decide which tool the model picks - cutting
  "use this for 'what courses cover X'" sent topic questions to SQL; usage
  rules weigh more in the system prompt than in tool text. A future attempt
  should undo one cut at a time.

### 35. Waiting notices, answer cleanup, follow-ups, departments in words — `done 2026-10-03`
- **Busy notices:** the Groq/OpenAI clients log "Retrying request ... in N
  seconds" before waiting out a 429; a log handler passes it, through a
  per-request context variable, to the stream that is waiting, which shows
  "Busy - waiting about N s for the AI model..." (the page had sat on
  "Running SQL..." for a minute). Local end to end on Groq: notices at each
  wait (11, 43, 48, 46 s).
- **Answer cleanup** (`tidy_answer`): internal names in plain words
  ("grad_credit = 'yes'" -> "open to graduate students", "the subjects
  table" -> "the department list", bare column names) and mangled link
  targets repaired ("?/instructor.html", "https://instructor.html").
- **Follow-ups:** the stream sends a "basis" event (the query or tool call
  the answer came from); the page keeps it with the turn and sends it back;
  the history shows it for the latest turn, and the prompt says to rerun it
  with the filter added and that "the second one" is the second item listed.
  End to end: "which of those are 400 level?" reran the Gies query with
  course_number LIKE '4%'.
- **Departments in words:** a department name written out ("intro
  psychology", "chemistry courses", "psych classes") counts as that
  department for the dropped-filter note and the latest-term rerun - only
  when the wording points at a department, so "engineering students" isn't
  the ENG department.
- **Measured:** answer-OK 95.5% / 94.6% / 97.3% over three runs (98.2% single baseline run). The misses (q12, q28, q40, q49, q64, q77, q91, q98) all also appear in earlier runs without these changes - run-to-run noise on advice questions, not a regression. q98's query, rerun through the wrapper, returns the right rows; the model misread them.
- **Still open:** broad questions still take 1-3 minutes on Groq (per-minute
  limit; the notices now show it); an answer once explained the link format
  to the student; a follow-up's list was labelled with one term without a
  term filter.
- **Deployed** d2793c1 (2026-10-03). Production check: "intro psychology
  sections in fall 2026" showed four busy notices (10, 35, 2, 38 s) and ran
  the PSYC query; a follow-up "which of those are online?" with a basis reran
  it with the meetings join and is_online = 1 (19 s).
- **Found in that check - empty final answer** (existed before: also logged
  2026-09-29 and 2026-10-01, all long Groq runs of 100-170 s): after the
  tools ran, the model's last message was empty, so the student got "I
  couldn't produce an answer for that." - and ask_log counted it as
  answered, with a sources footer. Proposed general fix: on an empty final
  after tool calls, one extra model call that writes the answer from the
  question and the tool results already gathered; and classify the fallback
  text as a failure, not an answer.

### 36. Request too large on broad questions; whole-department questions — `done locally 2026-10-09, eval and deploy pending`
- **Problem:** in production "tell me about badm sections" failed after 66 s
  with the generic error. Reproduced locally: Groq 413 "Request too large ...
  Limit 8000, Requested 8728" on the third agent step - the prompt, the
  history and two broad results no longer fitted in one request. Not a
  comprehension failure: the first query already filtered subject = 'BADM'.
  Without history the same question took 158 s and four queries and answered
  with a count per term. Same session: "sections of bad," -> "BAD does not
  exist"; "badm i mean" and one later question left `pending`; "try again"
  re-answered an earlier CS 225 question.
- **Done (general, each covers a class):**
  - a data budget per answer in estimated tokens, with room kept for the
    reply (`_start_data_budget`, `fit_budget`, `_CappedSQLDatabase._capped`;
    `LLM_REQUEST_TOKEN_LIMIT`) - applies to every question and every tool;
  - a `department_overview` tool for whole-department and whole-college
    questions, and a [Check] note that steers a cut-off whole-department
    SQL listing to it;
  - closest real codes for a department code that doesn't exist;
  - a 413 is reported as "pulled in more data than I can read at once" and
    tagged `error`.
- **Done (narrower - prompt wording):** one sentence saying a correction or
  retry ("badm i mean", "try again") is the latest question asked again.
- **Done, same day - more room in the same limit:** query results as a
  header line and "|"-separated rows (18-26% fewer tokens for the same rows,
  exact counts); history sent back without link targets, table rules, bold
  and the Sources footer, and capped at 700 characters when its query came
  with it (674 -> 429 tokens on the logged session). The token estimate was
  re-tuned against exact counts after Groq's count turned out to equal the
  o200k tokenizer's; the first version undercounted the new layout by ~10%.
  All offline-verified only: both change what the model reads, so they are
  part of the same eval run.
- **Checked:** offline suites pass, including the new
  `evals/test_department_overview.py` and the budget checks in
  `evals/test_agent_guards.py`. Live on Groq: "show me sections under badm"
  -> one `department_overview` call, 50 s, all 70 fall 2026 courses with
  section counts (before: 158 s, four queries). The with-history question
  stayed at 6,557 input tokens over four steps (before: refused at 8,728),
  then hit the daily token limit (200K) before its final step - the testing
  for this item used up the day's budget on the primary model.
- **Done, same day - one model call for whole-department answers:**
  `department_overview` returns its text as the answer (`return_direct`).
  Live through `/ask/stream`'s generator: "show me sections under badm" in
  5.7 s with all 70 fall 2026 courses; "Who teaches CS 225 this fall?" still
  answers through SQL. Found while doing it: the stream never matched the
  agent executor's end event (wrong name), so answers came only from
  streamed tokens; fixed, which also lets the iteration-cap message through.
  This may be part of the "empty final answer" finding in item 35 - not
  confirmed.
- **Measured, not acted on - daily capacity:** a two-step question is now
  ~9,600 tokens (4,631 + 4,709 input), so Groq's 200K tokens/day is about 20
  questions on the primary model before failover, not the 80-100 the comment
  in `_new_llm` gives (that dates from ~2,750 tokens per call).
- **Local environment:** `langchain-google-genai`, `google-genai` and
  `google-auth` (left from the Gemini support removed 2026-09-28) were
  uninstalled from the local `.venv`; they were never in `requirements.txt`,
  so Render was not installing them.
- **Done, same day - four latent bugs found by reading the pipeline:**
  - *Dropped streams left `pending` rows.* When the browser went away
    mid-answer the stream was cancelled, and in a cancelled task the
    `ask_log.finish()` await never ran; the row stayed `pending` and counted
    against the rate limits. Reproduced with a raw ASGI disconnect
    (`evals/test_request_guards.py`), then fixed with a shielded write and a
    new `cancelled` outcome (still counted: a model call was started).
  - *Yesterday's date in the prompt.* DATA NOTES ("Today is ...") and the
    agent built from them were cached for the life of the process; they are
    now cached per day. Deadline questions depend on that date.
  - *The empty-answer fallback was logged as an answer* (item 35's finding):
    it is now an error marker, so it isn't charged to the rate limit and
    gets no Sources footer. The cause of the empty final message is still
    open.
  - *The eval harness sized the data budget from the question alone*, not
    the question plus history as production does.
- **Done, same day - two more:**
  - *Sync status was cached until restart.* Which departments are synced for
    which term, department names, DATA NOTES (with the agents built from
    them) and the Sources footer's sync dates were cached for the life of
    the process, so after a sync the assistant went on saying "not synced
    yet". `refresh_catalog_caches()` now compares the newest scrape time and
    row count of `sections` at most every `CATALOG_CHECK_SECONDS` (600) and
    drops those caches when it changes; called from `build_agent`, off the
    event loop. One small query (24 ms on an open connection).
  - *Only the first code of `subject IN (...)` was read.*
    `sql_guard.subject_codes()` returns every code (also `s.subject`,
    `UPPER(subject)`), used by the unsynced note, the latest-term rerun, the
    unknown-department note, the whole-department note and the Sources
    footer. On the live data a Gies-wide fall 2026 query now gets "FIN, MBA
    has no fall 2026 rows"; before, it read ACCY alone and said nothing.
- **Eval run 2026-10-10** (`20261010T150017Z`, prod arm, OpenAI, Neon, 111
  questions; the data budget is off on OpenAI, so it is not exercised):
  answer-OK 95.5% (baseline runs 94.6-98.2%), result-match 86.4% loose
  (81.8-90.9%), refusals and no-data 100%. The five misses (q12, q28, q40,
  q64, q68) all also fail in baseline runs. Average latency 3.2 s (3.9-4.9 s);
  tokens 1.07M (0.83-0.99M), tool calls 130 (110-125).
  **Two regressions the scorer did not catch, found by reading answers:**
  - *The direct overview answered a question it shouldn't have.* q74 ("no
    class before 10am, which intro psychology sections would work") ran a
    rejected SQL query, then called `department_overview('PSYC')`, whose
    table became the answer - a course list for summer 2026, not sections by
    time. Scored OK. Also q35, q44, q78 (drop / credit questions) called it
    alongside `course_facts`; with two calls in one step the direct return
    doesn't fire, so those answers were right but carried a ~100-course list
    for nothing (part of the token rise).
  - *The "|" result layout is copied into answers as a table.* q40 and q98
    showed raw column names as headers (year | semester | subject ...), q40
    with start/end as minutes (720 | 770). None of the six baseline runs
    did either.
  Not deployable as is. Open: make the direct return conditional (first and
  only tool call) or drop it; change the row delimiter to one that doesn't
  read as a Markdown table.
- **Three more full runs the same day, and the outcome:**
  - Run 2 (`151427Z`; direct answer only as the first and only tool call;
    tab-separated rows with a header): 95.5%. q74 answered properly. q40 and
    q98 still showed raw columns - they now selected every column and listed
    them under the column names. q30 (ECE, unsynced fall) missed: right in
    substance, without the no-data wording.
  - Run 3 (`152527Z`; header line removed, unsynced wording fixed): 95.5%,
    no raw columns, q30 passes. But six repeats of q98 were right only 2
    times: same correct query, rows misread ("there are none").
  - Tuples restored as the default (`SQL_RESULT_LAYOUT=tabs` opts in): q98
    right 7 times in 8, q74 8 in 8, q40 1 in 8 (it is 5 of 12 in baseline
    runs).
  - **Final run (`155455Z`, tuples): answer-OK 96.4%** (twelve baseline runs:
    91.9-98.2%, median 95.5%), result-match 90.9% loose, refusals and
    no-data 100%, no raw columns or minutes in any answer, average latency
    2.9 s (3.7-4.9 s), tokens 1.06M (0.82-0.99M), tool calls 124 (108-128).
    Misses: q28, q40, q64 (fail in most baseline runs) and q39 (12 of 12 in
    baseline; 8 of 8 on repeat, so run-to-run variance). The overview
    answered directly twice, both whole-department questions (q30, q100).
  - **Still open:** the model calls `department_overview` beside
    `course_facts` on drop / credit questions (q35, q44, q78) - answers
    right, tokens wasted. The data budget is not exercised by this eval (it
    is off on OpenAI).
- **Live Groq checks, 2026-10-10 - both failed first, which found two holes:**
  "badm i mean" (as a follow-up) ended in the 413 again after 175 s
  (8,187 of 8,000 tokens): the schema tool's output (~1,200 tokens for three
  tables) was never counted in the data budget. And the model listed every
  BADM section, saw a fraction, ran three more queries and only then called
  `department_overview` (193 s, an answer mixing terms). Fixed: the schema
  tool is budgeted and counted; a whole-department listing too big to show
  returns only a pointer to the tool (no partial rows) and isn't counted, so
  the overview called next is still the direct answer; when the overview is
  not the answer, the model gets a compact copy (BADM 1,826 -> ~835 tokens)
  instead of a cut-off table. **After:** "badm i mean" with the production
  history 23.9 s, full list; "sections of bad," full list in 72 s (part of
  it per-minute waiting from the preceding test call); "badm i mean" after
  that answer 3.3 s. Not addressed: the typo answer doesn't say it read
  "bad" as BADM.
- **Merged and deployed 2026-10-10:** PR #12 (9be8571, deployed by the owner)
  and PR #13 (411ec7e, Manual Deploy from the dashboard, 1m37s).
  **Production check after 411ec7e:** "show me sections under badm" 3.3 s,
  all 70 courses (the question that started this item failed after 66 s);
  "sections of bad," the full BADM list in 92 s (two SQL listings turned
  into pointers, then the overview; most of the time is per-minute waits);
  "Who teaches CS 225 this fall?" correct in 81 s **on the backup model** -
  the primary's daily limit was spent by the day's testing.
  **Failed: "badm i mean" as a follow-up, on the backup model:** qwen
  answered with the agent's own opening line ("I know the schema from my
  instructions. I'll write the SQL...") in 2.7 s, logged as answered.
  Reproduced locally (2 of 3, then 3 of 3). Also seen there: the backup's
  limit of 1,000 output tokens a minute, which Groq words as "Request too
  large" and the app reported as a too-broad question.
  **Fixed, merged as PR #14 and deployed (40306b3, 1m43s). Production check,
  still on the backup model:** "badm i mean" as a follow-up now ends in "I
  couldn't produce an answer for that. Try asking it again..." after 31 s
  (was the echoed opening line, shown as an answer); "show me sections under
  badm" 3.4 s, all 70 courses, on the backup model too. The owner chose to
  stay on Groq's free tier for now (2026-10-10). **The fix:** an answer
  that is only the opening line is reported as no answer (error, not
  charged); the output-rate limit is reported as a rate limit. This makes
  the failure honest, not rare: the backup model still fails this follow-up
  every time locally. With ~20 questions a day on the primary model, the
  backup carries real traffic - the provider decision (item 2.3) is the fix.
- **What made production slower after the deploys (checked 2026-10-10):**
  not the code - the primary model's daily allowance. Measured: 196,714 of
  200,000 tokens used, a normal request refused, production on the backup
  model ("Who teaches CS 225 this fall?" 12 s the day before, 78-80 s). The
  site itself had 9 questions that day, all tests; the rest was two days of
  local live checks on the same Groq key. From the code: ~5% more tokens
  per call (4,400 -> 4,630 fixed), and "sections of bad," now takes three
  model calls (17 s and wrong before; 91 s and right).
  **Done on branch `local-groq-key`:** local runs and evals use
  `GROQ_API_KEY_DEV` when it is set (ignored on Render), and a local run on
  the production key says so once. The dev key must come from a different
  Groq account - limits are per organization. Not yet created by the owner,
  so nothing changes until it is.
- **"That question pulled in more data than I can read" in production,
  2026-10-10 (rows 456, 457: "grad courses in badm", "grad course related to
  business in badm"), on the backup model.** Cause: the data budget assumed
  the primary's ceiling for every model. The backup (qwen) allows 7,000
  input tokens a minute, counted in its own tokenizer (~13% more tokens for
  the same text: 5,224 for a bare question against 4,631), and 1,000 output
  tokens a minute; a request whose expected reply is larger is refused
  outright, and that expectation follows recent replies (after one
  1,141-token answer the next question was refused before it started).
  **Done on branch `backup-model-budget`:** a ceiling per model
  (`_MODEL_REQUEST_LIMITS`); the backup's replies capped at 900 tokens; and,
  for every model, once an answer's data budget is used up a further tool
  call returns "answer now from what you have" instead of a few more rows
  (the model had the full list after one query, ran two more, and the extras
  took the request to 7,055 of 7,000). Re-tested on the backup model: both
  questions answer (largest request 6,570 tokens); the long list is cut at
  the 900-token reply cap and says it is partial. Room for data on the
  backup is ~960 tokens, against ~1,470 on the primary - it cannot be
  raised on the free tier; the fixed prompt and tools are 5,200 of its
  7,000. Not fixed: "grad course in IS" on the backup still echoes the
  opening line (reported as no answer).
- **Confirmation eval (`163526Z`): answer-OK 98.2%**, result-match 95.5%
  loose, refusals and no-data 100%, no raw columns; misses q28 and q64 only
  (2 of 12 and 5 of 12 in baseline runs). Latency 3.0 s, tokens 1.03M.
- **Not yet done - before deploying:** (1) the eval run. This adds a tool
  and ~260 prompt/tool tokens per call, and item 34 showed tool and prompt
  text move answer-OK by points; it needs the usual full run against the
  98.2% / 95.5% baseline. (2) Live checks that could not be run after the
  daily limit: the with-history question to its final answer (after the
  [Check] note was added), "what courses are offered by gies", "sections of
  bad,", "badm i mean" as a follow-up. (3) eval rows for the class.
- **Still open, not addressed here:** every agent step re-sends ~4,630
  tokens against 8,000 a minute, so any question with more than one step
  waits ~30-60 s per extra step. The budget stops the failure, not the wait;
  the levers are a provider tier or model with a higher per-minute limit, or
  a smaller prompt (item 34 tried that and lost ~2.5 points). Rows left
  `pending` when the browser drops a stream (ids 440, 444): the stream's
  cleanup doesn't write the outcome when the request is cancelled, and a
  `pending` row counts against the rate limit.

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
