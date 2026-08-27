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

**Status: built and verified this session** (embedding generation live-tested; the Postgres/pgvector half is structurally complete but untested against a live Neon connection — see `DECISIONS.md`).

### How it works
1. **At scrape time (local, monthly):** for every course with a non-null `description`, embed the text with a self-hosted `fastembed` model (`BAAI/bge-small-en-v1.5`) and upsert the vector into a `course_embeddings` table in Neon (one row per distinct `(subject, course_number)`, not per section — the description is shared across sections).
2. **At query time (Render, online):** the LangChain agent has two tools:
   * `sql_query` (existing) — for instructor, CRN, enrollment, credit hours, counts, filters.
   * `course_content_search` (new) — embeds the question with the same local model, runs a `pgvector` cosine-distance search (`<=>` operator) against `course_embeddings`, returns the top-k matching course descriptions as context.
3. The agent's prompt is extended to explain when to use each tool (structured fact vs. "what is this course about" / "find courses about X").

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
