# Render Deployment & Sync Plan

This plan details the steps to make the UIUC Course Explorer Data Agent deployable on Render. It addresses persistent data storage, local-to-cloud syncing, rate-limiting/403 issues, and codebase changes.

---

## The Core Problems

### 1. Data Persistence on Render
Render's free tier Web Services run on ephemeral containers. Any local file (like the current `data/courses.db` SQLite database) is wiped whenever the service restarts, redeploys, or goes to sleep.
* **Paid solution:** Render Persistent Disk ($5/month). Allows keeping SQLite, but does not support scaling across multiple web instances, and SQLite files are difficult to backup/sync.
* **Free-tier solution:** Cloud PostgreSQL Database. We migrate the database backend to support PostgreSQL.
  * Render's own managed free Postgres now **expires after 30 days** (14-day grace period, then deleted) — not usable for a lifetime-free setup.
  * **Neon — recommended.** Free tier has no expiration date; compute autoscales to zero after 5 min idle but wakes on the next query (data stays, nothing gets deleted). Fits a low-traffic app well.
  * Supabase also offers a permanent free tier, but the project auto-**pauses after 7 days of no database activity** and needs a manual resume (or a periodic ping to prevent it) — worse fit for low, sporadic traffic than Neon.

### 2. The 403 Forbidden (Anti-DDoS) Block
UIUC's Course Explorer uses a Web Application Firewall (WAF) to protect its XML API.
* Hitting the XML API from cloud datacenters (like AWS, GCP, or Render's IP ranges) is highly likely to trigger automatic 403 Forbidden blocks.
* Hitting it too rapidly or concurrently from *any* single IP (even residential) will also trigger blocks.

---

## Proposed Architecture: Hybrid Sync

To solve both issues reliably without paying for expensive residential proxies, we propose the following **Hybrid Sync** architecture:

```mermaid
graph TD
    subgraph Local Machine (Residential IP)
        Scraper[scraper.py]
    end

    subgraph UIUC Servers
        Explorer[Course Explorer XML API]
    end

    subgraph Cloud
        DB[(Cloud PostgreSQL Database<br>Neon - free, no expiry)]
        RenderApp[FastAPI Web Service<br>Render Free Tier]
        Agent[LangChain SQL Agent]
    end

    %% Scraper data flow
    Explorer -->|XML Scrape (Allowed)| Scraper
    Scraper -->|Upsert Rows| DB

    %% Web UI/Agent flow
    RenderApp -->|Query Course Data| DB
    Agent -->|Read Schema & Query SQL| DB
    RenderApp -->|Ask Question| Agent
```

### Why this works:
1. **No 403 Blocks:** The scraper runs on your **local machine** (residential IP address), which is trusted by UIUC's WAF.
2. **Safe Database Synchronization:** The local scraper writes directly to your **Cloud PostgreSQL** database instead of a local SQLite file.
3. **No Downtime / Loss of Data:** Render reads directly from the cloud Postgres database. When the web service goes to sleep or is redeployed, the data is completely safe in Postgres.
4. **LangChain Compatibility:** LangChain's SQL Database agent supports PostgreSQL natively with no query changes required.

---

## Decisions Locked In

* **Database Host:** **Neon** — permanent free tier, fits low expected traffic.
* **Scraping cadence:** Manual/scheduled **local** run, roughly **once a month**, writing straight to Neon over `DATABASE_URL`. No cloud scraping automation (GitHub Actions runners hit the same datacenter-IP 403 problem as Render — not a real workaround) and no paid proxy/scraping API.
* **LLM:** **Groq** (`openai/gpt-oss-120b`) for answer synthesis.
* **Answer engine:** **Hybrid SQL + vector RAG.** The LangChain agent keeps its existing SQL tool for exact/structured lookups (instructor, CRN, open seats, counts) and gains a second **vector search tool** for semantic questions about course content ("what does CS 225 cover", "courses about machine learning"). The agent picks the right tool per question.
* **Embeddings:** **Self-hosted, open-source** — `fastembed` running `BAAI/bge-small-en-v1.5` (384-dim, ~68MB, MIT licensed). Groq turned out to have no embeddings API at all (confirmed live); this runs locally in both `scraper.py` and the Render app instead, no API key, no second provider, no rate limit.
* **Vector storage:** **`pgvector` extension on the same Neon database** — supported on Neon's free tier with no add-on, so no separate vector-DB service to run or pay for.

---

## Free LLM Options (Alternatives to OpenAI)

Since you want to avoid paying for an OpenAI API key, we can switch the LangChain agent to use a free API model. Since the application will be hosted on Render (which has limited CPU/memory and no GPU), we must use a cloud-hosted LLM rather than a local offline model (like Ollama). 

Here are the best free-tier cloud options:

### 1. Groq Cloud - *Recommended*
* **Models:** `openai/gpt-oss-120b`
* **Cost:** 100% Free Developer Tier
* **Limits:** ~30 Requests Per Minute, 1,000 Requests Per Day, 12K TPM — **and a separate 200,000 Tokens Per Day cap**, confirmed live (see `DECISIONS.md`). At ~1,800–2,500 tokens per real SQL-agent call (full schema context sent every time), the practical ceiling is closer to **~80-100 real questions/day**, not 1,000. Still fine for low traffic, but the binding constraint is tokens, not request count.
* **Capabilities:** Strong at tool-calling and SQL generation, verified live against this project's SQL agent. Groq is the fastest inference engine in the world.
* **Setup:** Get a free API key from [Groq Console](https://console.groq.com/). Set `GROQ_API_KEY` in the environment.

### 2. Google Gemini (via Google AI Studio)
* **Model:** `gemini-2.5-flash`
* **Cost:** 100% Free
* **Limits:** ~10 Requests Per Minute, RPD varies (Google has adjusted this repeatedly through 2026 — verify current quota at time of setup), ~250K Tokens Per Minute.
* **Capabilities:** Highly capable at tool calling and generating SQL. Very low latency.
* **Setup:** Get a free API key from [Google AI Studio](https://aistudio.google.com/). Set `GEMINI_API_KEY` in the environment.

### 3. OpenRouter (Free Models)
* **Models:** `meta-llama/llama-3-8b-instruct:free`, `liquid/lfm-40b:free`, etc.
* **Cost:** 100% Free
* **Limits:** Varies by model, potentially slower queues during peak times.
* **Setup:** Sign up at [OpenRouter](https://openrouter.ai/) and generate a free API key.

---

## Expanding Scraped Data

Researched what's actually available for free, no signup, from UIUC's public sources. Two real additions found; one promising lead turned out to be gated.

### 1. Meeting time / room / building — already fetched, currently discarded
`fetch_section_detail()` in `scraper.py` already hits the per-CRN detail endpoint
(`.../schedule/{year}/{semester}/{subject}/{course}/{crn}.xml`) but only extracts `instructor`
and `enrollmentStatus` from it. The same response also contains a `meetings` block per section with:
* `type` — meeting type (e.g. "Lecture-Discussion", "Laboratory")
* `start` / `end` — meeting start/end time
* `daysOfTheWeek` — which days it meets
* `roomNumber` / `buildingName` — physical location
* `instructors` — per-meeting instructor list (a section can have co-taught or split lecture/lab meetings, each with its own instructor)

Also available at the section level, not currently captured: `partOfTerm` (full term / first 8 weeks / etc.), `startDate`, `endDate`.

This needs **no new source or endpoint** — just parsing more of the XML the scraper already downloads. New columns: `meeting_type`, `meeting_days`, `meeting_start`, `meeting_end`, `building`, `room`, `part_of_term`, `section_start_date`, `section_end_date`. Since a section can have multiple meeting blocks (e.g. lecture + separate discussion time), this becomes a child table (`meetings`, FK to the section's `crn`+term) rather than flat columns on `sections`.

### 2. UIUC Grade Distribution dataset — new external source, free, official
[`wadefagen/datasets`](https://github.com/wadefagen/datasets) publishes a maintained CSV of UIUC grade distributions per section, going back to 2010:
[`gpa/uiuc-gpa-dataset.csv`](https://raw.githubusercontent.com/wadefagen/datasets/main/gpa/uiuc-gpa-dataset.csv)
* Columns: `Year, Term, YearTerm, Subject, Number, Course Title, Sched Type, Primary Instructor, A+..F, W, Students`.
* Since Spring 2025 this is **official data supplied directly by the University** (Urbana Senate item EP.25.072); older terms come from FOIA releases. Courses with ≤20 students are excluded (FERPA).
* Updated periodically (not on a fixed schedule) — no live API, just re-download the CSV.
* **License:** repo has no explicit LICENSE file. Fine for this kind of personal/non-commercial data-agent use (same public-record data cited openly elsewhere), but don't assume redistribution rights beyond that without checking with the maintainer first.

This adds real value for the RAG/agent layer: "which CS 225 sections/instructors historically have the easiest grading," grade trends over time, etc. — data the live Course Explorer API doesn't expose at all.

Plan: one-time (then periodic, alongside the monthly scrape) download + load into a `grade_distributions` table in Neon, joined to `sections` on `(subject, course_number, year, semester)` best-effort (instructor/section-type names won't always match exactly between the two sources, so join is advisory, not a strict FK).

### 3. Gen Ed categories / degree attributes / room capacity — investigated, not available for free
These looked promising but require UIUC's **authenticated** CISAPI tier (signup at `courses.illinois.edu/cisdocs/authentication`), separate from the public unauthenticated endpoints the scraper currently uses. Confirmed via the CISAPI GitHub client docs that gen-ed data specifically needs that access request. **Not pursuing now** — would mean an extra manual approval step outside the "stay free, no extra accounts beyond what's needed" goal. Revisit only if that's later worth requesting.

---

## RAG Layer (Hybrid SQL + Vector Search)

On top of the existing text-to-SQL agent, add a semantic retrieval tool for description-content questions.

**Status: built and verified end-to-end against live Neon + pgvector (2026-08-31).** Embedding generation, `course_embeddings` schema/HNSW index, batched backfill (`app/backfill_embeddings.py`, 4,748 rows), and the hybrid agent routing all confirmed live: a semantic question ("what courses cover machine learning") invokes `course_content_search` and returns real pgvector matches; a structured question ("who teaches CS 225") stays SQL-only. See `DECISIONS.md`. Note: Neon was populated by migrating the local SQLite file (`app/migrate_sqlite_to_neon.py`), not by scraping to Neon — the UIUC WAF soft-blocks a full scrape (see the Path B entry in `DECISIONS.md`).

### How it works
1. **At scrape time (local, monthly):** for every course with a non-null `description`, embed the text with a self-hosted `fastembed` model (`BAAI/bge-small-en-v1.5`) and upsert the vector into a `course_embeddings` table in Neon (one row per distinct `(subject, course_number)`, not per section — the description is shared across sections).
2. **At query time (Render, online):** the LangChain agent has two tools:
   * `sql_query` (existing) — for instructor, CRN, enrollment, credit hours, counts, filters.
   * `course_content_search` (new) — **multi-query**: one cheap non-streaming LLM call rewrites the topic and adds a few related facets, all embedded locally in one batch, each searched against `course_embeddings` via `pgvector` cosine (`<=>`), and the result lists fused with Reciprocal Rank Fusion into one de-duplicated top-k set. `RAG_MULTIQUERY=0` reverts to a single-query search. See `DECISIONS.md`.
3. The agent's prompt is extended to explain when to use each tool (structured fact vs. "what is this course about" / "find courses about X") and that the content tool self-expands, so one call is enough.

### Schema addition
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE course_embeddings (
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    description TEXT NOT NULL,
    embedding VECTOR(384),
    updated_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (subject, course_number)
);
CREATE INDEX ON course_embeddings USING hnsw (embedding vector_cosine_ops);
```
(`384` matches `BAAI/bge-small-en-v1.5`'s output dimension, confirmed live.)

### New/modified files
* **[NEW] `app/embeddings.py`** — wraps the local `fastembed` model (`embed_text`/`embed_texts`), plus the Postgres-only persistence/search functions (`init_course_embeddings_table`, `save_course_embedding`, `search_similar_courses`). Used by both the scraper (batch embed on scrape) and the agent (embed the incoming question). Pins the model's cache to `./model_cache` rather than fastembed's default temp-folder location, so `render.yaml`'s build step and the running app agree on where it lives.
* **[MODIFY] `app/scraper.py`** — after saving sections for a course, if the course has a description, embed it and upsert into `course_embeddings` (skip if `DATABASE_URL` isn't set, since SQLite has no `pgvector`).
* **[MODIFY] `app/agent.py`** — registers `course_content_search` as a second tool via `create_sql_agent`'s `extra_tools` parameter, update `SYSTEM_CONTEXT` to describe when to use it.
* **[MODIFY] `app/api.py`** — loads the embedding model at FastAPI startup (Postgres only) rather than lazily on first use, so the load overlaps Render's own container boot instead of a user's first request.
* **[NEW] `render.yaml`** — build step pre-downloads the embedding model so it's already cached before the app serves traffic (see cold-start reasoning in `DECISIONS.md`).
* **Local/SQLite fallback:** vector search tool is only registered when `DATABASE_URL` (Postgres) is active — local SQLite dev mode keeps working with SQL-only, no `pgvector` dependency required locally.

---

## Proposed Code Changes

We will modify the codebase to support **both SQLite and PostgreSQL** dynamically. If a `DATABASE_URL` is set in the environment, the app uses PostgreSQL; otherwise, it falls back to local SQLite. This keeps local testing simple.

### 1. Database Adaptor Layer
#### [NEW] [db.py](file:///d:/PythonProject/course-explorer-agent/app/db.py)
* Add a unified database client that checks for `DATABASE_URL`.
* Abstract SQLite vs PostgreSQL syntax differences (such as using `%s` placeholders for PostgreSQL and `?` for SQLite).
* Manage connection pooling and table/index creation.

### 2. Scraper Adaptation
#### [MODIFY] [scraper.py](file:///d:/PythonProject/course-explorer-agent/app/scraper.py)
* Replace direct `sqlite3` imports and connection calls with the unified `db.py` helper.
* Translate SQLite-specific table creation queries (`AUTOINCREMENT` -> `SERIAL`, `scraped_at TEXT` -> `TIMESTAMP`) to be compatible with PostgreSQL.
* Add options for request rate-limiting (e.g. delay-tuning) to ensure scanning of the full UIUC catalog does not trigger blocks even locally.
* Extend `fetch_section_detail()` to also parse the `meetings` block (`type`, `start`, `end`, `daysOfTheWeek`, `roomNumber`, `buildingName`, per-meeting `instructors`) and the section-level `partOfTerm`/`startDate`/`endDate` fields already present in the response, instead of only reading `instructor`/`enrollmentStatus` from it.
* Add a new `meetings` table (FK on `year, semester, subject, course_number, crn`) since a section can have multiple meeting blocks.
* Add `app/load_grades.py` — one-off/periodic loader that downloads `uiuc-gpa-dataset.csv` from `wadefagen/datasets` and upserts it into a new `grade_distributions` table.

### 3. API Adaptation
#### [MODIFY] [api.py](file:///d:/PythonProject/course-explorer-agent/app/api.py)
* Replace `get_conn()` and `run_query()` with database-agnostic versions from `db.py`.
* Ensure static files and routes are fully compatible with production settings.
* Extend `/sections` response (and `SectionOut`) with the new meeting fields (joined from `meetings`); add grade distribution to `/courses/{subject}` where available.

### 4. Agent Adaptation
#### [MODIFY] [agent.py](file:///d:/PythonProject/course-explorer-agent/app/agent.py)
* Update database connection URI to parse `DATABASE_URL` (translating it to `postgresql+psycopg2://...` or `sqlite:///...` for SQLAlchemy).
* Support dynamic LLM switching: if `GROQ_API_KEY` is present, initialize `ChatGroq` with `openai/gpt-oss-120b` (preferred - highest free daily quota); else if `GEMINI_API_KEY` is present, initialize `ChatGoogleGenerativeAI` with `gemini-2.5-flash`; otherwise fallback to `ChatOpenAI` with `gpt-4o-mini`.

### 5. Deployment Configurations
#### [NEW] [render.yaml](file:///d:/PythonProject/course-explorer-agent/render.yaml)
* Add a Render Blueprint specification to easily spin up the FastAPI service and (optionally) a PostgreSQL instance with one click.
#### [MODIFY] [requirements.txt](file:///d:/PythonProject/course-explorer-agent/requirements.txt)
* Add `psycopg2-binary` for Postgres support.
* Add `pgvector` (Python client bindings for the Postgres extension) for vector search.
* Add `langchain-groq` for the LLM + embeddings; keep `langchain-google-genai` as an optional fallback.

---

## Verification Plan

### Automated Verification
1. Run local test suite against a local SQLite database to ensure backward compatibility.
2. Run tests against a local or test PostgreSQL instance (with mock credentials) to ensure Postgres query generation is correct.
3. Verify LangChain SQL Agent queries on both databases.
4. Verify `course_embeddings` upsert (embed a known description, confirm the vector round-trips and cosine search returns it as the top match for a paraphrased query).

### Manual Verification
1. Run scraper locally pointing to the cloud PostgreSQL database.
2. Verify that the table schema (`sections` + `course_embeddings`) is created correctly in Postgres.
3. Access the deployed Render URL and perform search and AI queries to verify data loading.
4. Ask a structured question ("who teaches CS 225") and confirm the SQL tool is used.
5. Ask a content question ("what courses cover machine learning") and confirm the vector search tool is used and returns relevant courses.


---

## Open issues: resolution plan (2026-09-19)

Written after the Gen Z UI refresh and the assistant fixes (iteration cap,
`enrollment_status` codes, prompt gaps). Nothing below is started. The *why*
behind what already shipped is in `DECISIONS.md` ("Gen Z refresh" and
"Assistant fixes" sections, both dated 2026-09-19). Status values: `todo`,
`blocked` (needs something outside the code), `user` (needs the owner).

Suggested order: 3 and 3b first (they decide whether the assistant fixes hold and stay fast in production), then 2, then 4-7
(assistant quality), then 8-11 (data, UI, structure), then 12-13 (housekeeping).

### 1. OpenAI key exposure — `closed` (owner decision, 2026-09-19)
- **What happened:** the `OPENAI_API_KEY` line of the gitignored `.env` was
  printed into a Claude Code session transcript on 2026-09-19.
- **Decision:** no rotation. The key is a local-only fallback; production runs
  on `GROQ_API_KEY`, already set in the Render dashboard (`render.yaml` lists
  `OPENAI_API_KEY` only as an optional, unsynced fallback, and the app prefers
  Groq whenever its key is present). Risk accepted: the key is still valid and
  sits in that transcript, so revisit if the transcript is ever shared.
- **Follow-on:** the local `.env` has the Groq line commented out (the daily
  cap was hit on 2026-09-10), so local runs use OpenAI while production uses
  Groq. See item 3.

### 2. Commit the pending work in reviewable pieces — `todo`, wait for go-ahead
- **Problem:** roughly 20 files are uncommitted (UI refresh, assistant fixes,
  evals, code-critic agent, `qa_log.txt`). One giant commit would be
  unreviewable and hard to revert.
- **Plan:** three commits, none pushed until approved. (a) `app/agent.py`,
  `app/ask_log.py`, `evals/*`: assistant fixes plus gold rows q25-q29 and
  `test_agent_guards.py`. (b) `static/*` and `.claude/agents/code-critic.md`: UI
  refresh. (c) `DECISIONS.md`, `implementation_plan.md`, `qa_log.txt`. Run the
  six offline evals and `node --check` on the JS first, and run `code-critic`
  once more on the final diff. Leave `plan.txt` alone (it is the user's
  untracked note).
- **Done when:** three commits exist locally and the working tree is clean
  apart from `plan.txt`.

### 3. Re-verify the assistant fixes on the production model — `partly done 2026-09-19`, continue
- **Done so far:** local `.env` now uses Groq (`openai/gpt-oss-120b`, the
  production model) and `_build_llm()` prefers Groq, then OpenAI, then Gemini.
  Five QA questions were re-run once on Groq after trimming the prompt: no
  prerequisites, humanities gen-ed, top CS instructor and 400-level CS courses
  answered correctly (the no-prerequisites list matched the gold query exactly);
  "open sections this fall" gave the right "not published" answer on the first
  run and hit a rate limit on the second. That is one run per question, not a
  pass rate.
- **Still to do:**
  1. Run the gold set with `evals.run --arms prod --db env --provider groq`
     and `--provider openai` and a repeat count (add `--repeats N`, default 1),
     reporting per-question pass rate. Spend Groq tokens at a quiet time and use
     `--limit` (an eval run draws on the same daily budget as students).
  2. Compare providers; if Groq is worse on a specific pattern, add the
     matching prompt rule.
  3. Consider `SQL_PIPELINE=critic` in production: the Critic/Repair loop was
     built for this failure (dropped filters, wrong columns). Decide from the
     repeated-run numbers and record it in `DECISIONS.md`.
- **Done when:** per-question pass rates over at least 3 repeats on Groq are in
  `evals/RESULTS.md` and the default provider/mode is chosen from them.

### 3b. Groq free-tier per-minute token limit throttles the assistant — `todo`, found 2026-09-19
- **Problem:** Groq's on-demand tier for `openai/gpt-oss-120b` returned
  "TPM limit 8000, used 4120, requested 6111". One agent step sends about
  6,100 tokens (the ~2,550-token system prompt plus tool schemas, question and
  earlier tool results), so a second step inside the same minute is refused and
  LangChain waits and retries. Observed latencies for one question ranged from
  4 s to 154 s, and one question ended in "rate limit was hit". Anything that
  needs 2+ steps, or two students asking within a minute, is exposed. My
  first prompt additions made it worse (system prompt +816 tokens, about +40%);
  they were compressed to +547.
- **Plan (in order of effort):**
  1. Slim the system prompt further without changing behaviour: the
     `academic_calendar` paragraph, the RAG-tool paragraph and the formatting
     rules are the longest; target under 2,000 tokens total, verified against the
     gold set so no answer regresses.
  2. Cut steps: drop `sql_db_list_tables`, `sql_db_schema` and
     `sql_db_query_checker` from the toolset (also fixes plan item 5), so a normal
     question is one query call plus the answer.
  3. Runtime failover: if Groq returns a 429, retry the same question once on
     the next configured provider (OpenAI) instead of showing "rate limit was
     hit". This is only useful in production if `OPENAI_API_KEY` is set there,
     which costs money; the owner decides. Record the decision in `DECISIONS.md`.
  4. Check whether upgrading Groq to the Dev tier is cheaper than any of the
     above (higher TPM, pay per token).
- **Done when:** three consecutive multi-step questions complete in under 30 s
  each on Groq with no 429, or the chosen failover is in place.

### 4. Evals run against the wrong database by default — `todo`
- **Problem:** `evals/run.py` defaults to `--db sqlite` (local `data/courses.db`,
  14,714 sections) while the live app reads Neon (19,848 sections, different
  fall-2026 data). Numbers from the default run do not describe what students
  get. The new gold rows were only spot-checked against Neon by hand.
- **Plan:** make `--db env` the default (fall back to sqlite only if
  `DATABASE_URL` is unset), print which database was used at the top of every
  results file, and run the full 29-row set once on Neon. Note: `app/db.py`'s
  `execute()` treats `%` as a parameter marker on Postgres, so gold SQL with
  `LIKE '1%'` needs `%%` there (or `substr()`); handle it in the harness rather
  than rewriting the gold queries.
- **Done when:** the full run completes on Neon and its summary names the
  database.

### 5. Malformed instructor links and ignored tool rules — `todo`
- **Problem:** the model sometimes writes `https://instructor.html?name=...`
  instead of `/instructor.html?name=...` (broken link), and sometimes calls
  `sql_db_list_tables` / `sql_db_schema` despite the prompt saying not to,
  costing a step each.
- **Plan:** stop relying on the prompt. (a) Remove those two tools (and
  `sql_db_query_checker`) from the agent's tool list in `build_agent()`; the
  schema is already in the prompt. (b) Add a small post-processor for the final
  answer that rewrites `](https://instructor.html` and `](https://?course=` to
  the site-relative form, with a unit test in `test_agent_guards.py`.
- **Done when:** a 10-question sample never shows those tool calls, and the
  link test passes.

### 6. Long, noisy table answers ("No 8ams") — `todo`
- **Problem:** the late-start question returns about 176 raw meeting rows with
  repeated courses and CRN "N/A" (it queried `meetings` without joining
  `sections`). Correct but unhelpful.
- **Plan:** add a prompt recipe: for "which courses start after X", group by
  course (one earliest qualifying meeting each), join `meetings` to `sections`
  on the five-column key so CRN and instructor exist, and show a count plus the
  first ~15 with "N more". Add a gold row for it. Optionally reword the chip
  once the answer is tidy.
- **Done when:** the chip's answer fits on one screen and has CRNs.

### 7. Verify the `P` status label and the "only Tue/Thu" meaning — `blocked` on a data sample
- **Problem:** `P` is labelled "Pending" in the UI and prompt by inference from
  UIUC's `sectionStatusCode`; not confirmed. Separately, "meets only on
  Tuesdays and Thursdays" was not verified at course level (a course can have a
  lecture on TR and a discussion on another day).
- **Plan:** pull one raw section XML for a `P` section (the scraper already
  fetches them) and read the code's meaning from UIUC's schema or docs; fix the
  wording if wrong. For Tue/Thu, decide the definition (every meeting TR vs.
  lecture TR), write the SQL, and add it as gold row q30.
- **Done when:** the label is confirmed or corrected and q30 exists.

### 8. Grade and instructor-ranking data are empty — `blocked` on upstream
- **Problem:** `grade_distributions` and `teachers_ranked_excellent` have 0 rows
  in Neon, so GPA questions and the Grade History tab correctly say "no data".
  The GPA chips were removed for this reason.
- **Plan:** check whether the upstream datasets (the sources behind
  `load_grades.py` and `load_tre.py`) currently publish data. If so, run the
  loaders locally against Neon (writes only happen from the local machine, per
  `DEPLOYMENT.md`), then restore the "GPA boosters" chip and add gold rows. If
  upstream is empty, leave as is and add a short "not published yet" note to the
  Grade History tab so it does not look broken.
- **Done when:** either grade rows exist and the chip is back, or the tab
  explains the gap.

### 9. Panels stay hidden after a hard scroll jump — `todo`
- **Problem:** with motion on, jumping straight past below-the-fold panels
  (Home/End, anchor links) leaves them at `opacity: 0` until scrolled back into
  view, because the IntersectionObserver never sees them cross its threshold.
- **Plan:** in `motion.js`, add a passive `scroll` listener (throttled with
  `requestAnimationFrame`) that marks any `.reveal` element whose top is above
  the viewport bottom as `.in`, plus a 3 s fallback timer that marks the rest.
  Test over CDP: load, `scrollTo(0, scrollHeight)`, assert no
  `.reveal:not(.in)` remains above the fold.
- **Done when:** the CDP check passes with motion enabled.

### 10. Duplicated theme tokens and copy-pasted nav — `todo`
- **Problem:** dark-mode tokens are defined in both `style.css` and
  `theme-genz.css`; the six-link nav (now with icons and short labels) is
  copy-pasted into 7 pages, so any nav change touches 7 files. The code critic
  ranked this the highest-value structural cleanup.
- **Plan:** (a) move the final token values into `style.css`'s `:root` and dark
  blocks and reduce `theme-genz.css` to skin and motion rules; check both themes
  visually on every page. (b) Generate the nav from one source: a small build
  step (`scripts/build_pages.py`) that injects `static/partials/nav.html` into
  each page (no runtime cost, works without JS), rather than a template
  engine. Add a check to the offline evals that every page has identical nav
  markup.
- **Done when:** one place defines tokens, one place defines the nav, and the
  consistency check passes.

### 11. Chip set and small UI polish — `todo`
- **Problem:** "Gen-ed finder" nearly duplicates the default "QR gen-ed" chip,
  and the phone chip row hides its scrollbar, so the swipe hint relies on the
  clipped last chip.
- **Plan:** replace "Gen-ed finder" with a distinct answerable question (for
  example earliest class time for a named course, once item 6 lands); add a
  subtle fade on the right edge of the chip row on phones. Re-run the phone-width
  check afterwards.
- **Done when:** no duplicate chips and the fade renders at 390px and 320px.

### 12. Keep the browser checks so they can be re-run — `todo`
- **Problem:** motion and phone-width behaviour were verified by hand over the
  DevTools protocol on 2026-09-19; nothing re-runs it.
- **Plan:** save those scripts as `evals/ui_smoke.py`: reveal, stagger, shake,
  ripple, confetti, spotlight, no horizontal overflow at 390 and 320, and
  reduced-motion disabling each effect. It needs Chrome and a running server,
  so run it manually before UI commits rather than in CI.
- **Done when:** `python -m evals.ui_smoke` prints all checks passing with a
  server on `:8765`.

### 13. Housekeeping — `todo`
- Line endings: git warns "LF will be replaced by CRLF" on nearly every file.
  Add a `.gitattributes` (`* text=auto eol=lf`) so diffs stay clean.
- `qa_log.txt` grows with every QA run; decide whether it stays tracked or is
  gitignored.
- `admin.html` was deliberately left out of the refresh; re-check it visually
  once the tokens are consolidated (item 10), since it shares `style.css`.

### 3c. Evaluate alternative Groq models, then decide on failover — `todo`, 2026-09-20
- Model name is now an env var (`GROQ_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`); `python -m evals.run --provider groq --model openai/gpt-oss-20b -y` scores a candidate on its own daily Groq budget.
- Candidates: `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`. Note: prod, dev and evals share one Groq key, so eval runs eat production budget.
- Then: 429 failover from 3b, and trimming `SYSTEM_CONTEXT` (about 2,550 tokens, sent every step). Decide later.
- **2026-09-20 update:** 429 failover to `GROQ_FALLBACK_MODEL` (default qwen/qwen3.8-27b) is implemented (see `DECISIONS.md`); still open: measure qwen's per-minute limit and its full-set accuracy.
