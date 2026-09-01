# Project Bible — UIUC Course Explorer Data Agent

The single orientation document for this repository. It explains what the
project is, why it is shaped the way it is, how the pieces fit together, and
how it is operated. It is deliberately broad and shallow: for the *why*
behind any individual decision, follow the cross-reference into `DECISIONS.md`;
for step-by-step deployment, see `DEPLOYMENT.md`; for an illustrated view of
the storage model and request path, open `docs/architecture.html`.

Last reviewed: 2026-08-31.

---

## 1. What this is

A small dataset-and-agent project built on top of UIUC's public Course
Explorer API. It:

1. **Scrapes** course, section, instructor, meeting, and enrollment data for
   a rolling window of terms from UIUC's public XML API, plus a few
   supplementary public datasets (grade distributions, "Teachers Ranked as
   Excellent", Gen Ed categories).
2. **Structures** it into a documented relational schema that works
   unchanged on both SQLite (local dev) and Postgres/Neon (production).
3. **Exposes** it through a FastAPI backend with a browser UI for
   browsing/filtering sections and asking questions.
4. **Answers plain-English questions** through a LangChain agent that writes
   and runs its own SQL for structured lookups and does pgvector-backed
   semantic search for content questions.

### Why it exists

It is a work sample for AI Engineer / Forward-Deployed Engineer roles. It
walks the same core loop as most applied data work: pull from an external
source, structure it into a documented schema, expose it through an API, and
let a non-technical user ask a question in plain language instead of writing
SQL by hand.

---

## 2. Operating model and guiding principles

These constraints shaped nearly every technical decision. Understand them
first and the rest of the design follows.

- **Lifetime-free hosting.** No paid tiers. Render free web service + Neon
  free Postgres. This ruled out Render Persistent Disk, Render's own
  time-limited free Postgres, and Supabase's auto-pausing free project.
  (`DECISIONS.md` — "Hosting".)
- **Scraping runs locally, never in the cloud.** UIUC's Course Explorer sits
  behind a WAF that 403s datacenter IPs (Render, GitHub Actions, AWS/GCP).
  The scraper runs from a residential IP on the operator's machine and
  writes straight to Neon over `DATABASE_URL`. (`DECISIONS.md` — "Scraping".)
- **Manual, roughly monthly refresh.** No cron, no scheduled full re-scrape.
  `scraper.py` takes explicit `--year/--semester` per run. `terms.py`
  carries a hand-edited "current term" that rolls forward manually.
- **One query text, two backends.** Every SQL string in the codebase is
  written once with `?` placeholders and SQLite-ish syntax; `app/db.py`
  translates at execution time. Nobody needs Postgres running to develop.
- **Low traffic.** The app is a portfolio piece, not a service with users.
  Designs favour simplicity and zero standing cost over throughput or
  horizontal scale.
- **Self-hosted embeddings.** No embeddings API (Groq has none; adding
  OpenAI/Cohere would mean a second provider and key). `fastembed` runs
  `BAAI/bge-small-en-v1.5` locally in both the scraper and the web app.

---

## 3. Architecture at a glance

```
 Local machine (residential IP)                 Cloud (all free tier)
 ┌───────────────────────────┐                  ┌─────────────────────────────┐
 │ scraper.py                │   XML (allowed)  │  Neon Postgres              │
 │ load_grades / load_tre /  │◀──────────────── │   - relational tables       │
 │ load_geneds / backfill    │                  │   - pgvector: course_embeddings
 │        │  upsert + embed   │ ───────────────▶ │                             │
 └────────┼──────────────────┘   DATABASE_URL   │            ▲                │
          │                                     │            │ SQL + vector   │
          ▼                                     │  ┌─────────┴──────────────┐ │
   data/courses.db (SQLite,                     │  │ FastAPI app (Render)   │ │
   local dev fallback only)                     │  │  api.py  +  agent.py   │ │
                                                │  │  static/ browser UI    │ │
                                                │  └────────────────────────┘ │
                                                └─────────────────────────────┘
```

- The **scraper side** and the **serving side** never talk directly. Neon is
  the only shared state. When Render sleeps or redeploys, data is safe in
  Neon.
- **SQLite is a local-dev convenience only.** Production is always Postgres.
  Vector search is Postgres-only by design (no vector type in SQLite), so
  the RAG tool is inert on a local SQLite setup.

---

## 4. Components

### Serving path (`app/`)

| File | Role |
|---|---|
| `api.py` | FastAPI backend. Security-header middleware, generic exception handler, all HTTP endpoints, `/ask` guardrails (length cap, rate limit), static file serving. |
| `agent.py` | The LangChain hybrid agent. Dynamic LLM provider selection, SQL toolkit over an allow-listed set of tables, the `course_content_search` RAG tool with multi-query expansion + RRF merge, `SYSTEM_CONTEXT` scope guardrails, windowed conversation history, `ask()` and `astream_answer()`. |
| `db.py` | The dual-backend connection wrapper. `?`→`%s` translation, `upsert()`, `existing_columns()`, DDL keyword helpers. The one place either backend is connected. |
| `embeddings.py` | Self-hosted embedding model wrapper. `course_embeddings` table + `CREATE EXTENSION vector` + HNSW index setup, `embed_texts()`, `search_similar_by_vector()`. No-ops on SQLite. |
| `ask_log.py` | Append-only activity log of `/ask` calls (timestamp, client IP, question, outcome classification, answer preview, latency). Backs `/admin/ask-log`. |
| `sync_requests.py` | Per-department "please refresh this" counter. Backs `POST /sync/request` and `GET /sync/status`; the operator reads it to decide what to re-scrape next. |
| `terms.py` | Shared definition of the active term window: current term ±2, walking the spring/summer/fall cycle. Used by the history-loading scripts so they don't hoard decades of data. |

### Ingestion path (`app/`, run locally)

| File | Role |
|---|---|
| `scraper.py` | The Course Explorer XML scraper → `sections` + `meetings`. Concurrent per-course fetches, `--fast` / `--concurrency` / `--skip-recent` / `--section-delay` flags, automatic schema migration, resumable, embeds descriptions as it goes when pointed at Postgres. |
| `load_grades.py` | Loads historical grade distributions from wadefagen/datasets → `grade_distributions`. |
| `load_tre.py` | Loads "Teachers Ranked as Excellent" → `teachers_ranked_excellent`. |
| `load_geneds.py` | Loads Gen Ed category assignments → `gen_ed_categories`. |
| `load_catalog_snapshot.py` | Loads a one-term catalog description snapshot (historical backfill). |
| `backfill_embeddings.py` | Standalone: (re)embed every course description already in the DB into `course_embeddings`. Used after a bulk load or an embedding-model change. |
| `migrate_sqlite_to_neon.py` | One-shot: copy a fully-populated local SQLite DB into Neon. This is how production data was first populated (Path B), instead of scraping straight to the cloud. |
| `sync_requests.py` (CLI) | Also runnable as a CLI to inspect/clear the department request queue. |

### Frontend (`static/`)

| File | Role |
|---|---|
| `index.html` | Single-page browser UI. The assistant (streamed answers, Markdown rendering) is the headline; section browsing/filtering is secondary. |
| `freshness.html` | Data-freshness detail subpage (per-term / per-subject scrape recency). A summary holder sits on the home page. |

### Docs

| File | Role |
|---|---|
| `README.md` | Quickstart: setup, scrape, run, ask, schema table, repo layout. |
| `DECISIONS.md` | The running log of *why*. Every architecture / data-source / scope decision with its rationale and the rejected alternatives. Append-only, dated sections. |
| `DEPLOYMENT.md` | Render + Neon runbook: the hybrid-sync rationale, env vars, first-populate steps, monthly-refresh procedure, 403 mitigation options. |
| `implementation_plan.md` | The build plan — *what* to build and in what order. |
| `docs/architecture.html` | Illustrated: the two storage layers, the schema, the request path, the multi-query RAG loop. |
| `docs/PROJECT_BIBLE.md` | This file. |
| `qa_log.txt` | Output log from the `course-agent-qa` subagent — real answer-quality regression checks. |
| `security_findings.md` | Dated red-team passes from the `course-app-redteam` subagent, with fixes. |

---

## 5. Data model

All tables live in one database. Types shown are the logical intent; `db.py`
maps DDL keywords per backend (`SERIAL`/`AUTOINCREMENT`, `TIMESTAMP`/`TEXT`).

### `sections` — one row per course section per term
Populated by `scraper.py`. `UNIQUE(year, semester, subject, course_number, crn)`.

`year`, `semester` (lowercase), `subject` (uppercase code), `course_number`,
`course_label`, `crn`, `section_name`, `instructor` (nullable — detailed mode
only), `enrollment_status` (nullable, not live — value at scrape time),
`credit_hours` (as published, e.g. "3 OR 4 hours"), `description` (catalog
text, nullable, same for every section of a course), `part_of_term`,
`section_start_date`, `section_end_date`, `scraped_at`.

### `meetings` — one row per meeting block per section
Populated by `scraper.py` from XML it already fetches. A section can have
several (lecture + separate discussion, co-taught splits).

`year`, `semester`, `subject`, `course_number`, `crn`, `meeting_type`,
`days_of_week`, `start_time`, `end_time`, `building`, `room`, `instructor`.

### `grade_distributions` — historical grade outcomes
Populated by `load_grades.py` from wadefagen/datasets. Per-instructor,
per-term letter-grade counts (`a_plus` … `f`, `w`, `students`) plus
`sched_type`, `primary_instructor`, `course_title`. Keyed
`UNIQUE(year, term, subject, course_number, sched_type, primary_instructor)`.

### `teachers_ranked_excellent` — TRE list
Populated by `load_tre.py`. `year`, `term`, `unit`, `last_name`,
`first_name`, `role`, `ranking`, `course_number`. Join to instructors is
best-effort (name-only) — see the join caveat in `DECISIONS.md`.

### `gen_ed_categories` — Gen Ed requirement tags
Populated by `load_geneds.py`. One row per course:
`acp`, `cs`, `hum`, `nat`, `qr`, `sbs` category columns, plus snapshot term.
`UNIQUE(subject, course_number)`.

### `course_embeddings` — pgvector index (Postgres only)
Populated by `scraper.py` inline and by `backfill_embeddings.py`.
`PRIMARY KEY (subject, course_number)`, `description`,
`embedding VECTOR(384)`, `updated_at`. HNSW index, cosine ops.
Does not exist on SQLite.

### `ask_log` — `/ask` activity log
Populated by `ask_log.py`. `ts`, `client_ip`, `question`, `outcome`
(classified: answered / refused-scope / rate-limited / error / …),
`answer_preview`, `latency_ms`.

### `sync_requests` — department refresh queue
Populated by `sync_requests.py`. `subject` (PK), `pending_count`,
`last_requested_at`. Read-modify-write increment; a lost count under
concurrency is acceptable (operator ranks by relative magnitude).

The SQL agent is only shown five of these tables (`sections`, `meetings`,
`grade_distributions`, `teachers_ranked_excellent`, `gen_ed_categories`) —
`INCLUDED_TABLES` in `agent.py`. `course_embeddings` is reached only through
the dedicated RAG tool; `ask_log` and `sync_requests` are internal.

---

## 6. Data sources

| Source | Feeds | Access | Notes |
|---|---|---|---|
| Course Explorer XML API (`courses.illinois.edu/cisapp/explorer/...`) | `sections`, `meetings`, descriptions | Public, unauthenticated, **WAF-blocked from datacenters** | Schedule endpoints for subjects/courses/sections; a separate catalog module for descriptions. Detailed mode costs 1 extra request per course (description) + 1 per section (instructor/enrollment/meetings). |
| wadefagen/datasets (GitHub) | `grade_distributions`, historical catalog snapshot | Public CSV | Pre-scraped UIUC GPA dataset. One term used for the historical description backfill. |
| "Teachers Ranked as Excellent" list | `teachers_ranked_excellent` | Public | Name-only join to instructors. |
| Gen Ed category listing | `gen_ed_categories` | Public | Snapshot, not per-term tracked. |

Explicitly rejected sources: live seat-availability tracking (re-scrape
instead), RateMyProfessor scraping. See `DECISIONS.md`.

---

## 7. Request flows

### `POST /ask` (and `POST /ask/stream`)
1. `api.py` applies guardrails: max question length, per-IP rate limit.
2. Optional windowed conversation `history` from the browser (a list of
   `{q, a}` turns, capped at 20 in the request, last 3 actually used) is
   formatted by `agent.py` `_format_history()` and prepended to the agent
   input as a context-only block. **The server keeps no session state.**
3. The LangChain agent chooses tools:
   - **SQL toolkit** for structured questions (who teaches X, how many
     sections, open seats, grade history, Gen Ed tags). It writes and runs
     SQL against the allow-listed tables.
   - **`course_content_search`** for content/topic questions ("what does
     CS 225 cover", "courses about machine learning"). This tool:
     a. makes one cheap LLM call to expand the topic into ~3 facets
        (`RAG_MULTIQUERY`, `RAG_SUBQUERIES`),
     b. embeds each facet locally,
     c. runs a pgvector similarity search per facet (`RAG_K_PER`),
     d. merges the result lists with Reciprocal Rank Fusion and returns the
        top `RAG_K_RETURN`.
4. `SYSTEM_CONTEXT` keeps the agent in scope: it refuses general knowledge,
   trivia, coding requests, and prompt-injection attempts ("ignore previous
   instructions") rather than answering them.
5. The answer is returned as Markdown. `/ask/stream` sends it back as
   Server-Sent Events; `static/index.html` renders it incrementally. Tables
   are no longer the default answer shape — prose unless the question wants a
   table.
6. `ask_log.py` records the call and its classified outcome.

### `GET /sections`, `/subjects`, `/courses/{subject}`, `/stats`, `/freshness`
Plain filtered reads straight from the relational tables via `db.py`. No LLM.

### `POST /sync/request` → `GET /sync/status`
A browser user viewing a stale/absent department can request a refresh; the
counter bumps. The operator reads `/sync/status` (or the CLI) before the next
local scrape to decide what to pull. This is the "demand-driven,
department-level refresh" model — there is no scheduled full re-scrape.

### `GET /admin/ask-log`, `GET /schedule/conflicts`, `GET /courses/{s}/{n}/grade-trend`
Admin/analytics reads over `ask_log` and the relational tables.

---

## 8. The agent in detail (`agent.py`)

- **LLM provider is auto-selected** from whichever key is set, in order:
  `GROQ_API_KEY` (preferred — free, best daily quota), `GEMINI_API_KEY`,
  `OPENAI_API_KEY`. Model: Groq `openai/gpt-oss-120b`.
- **Groq's binding limit is tokens per day (~200k), not request count.** The
  full schema context is sent every call (~1.8–2.5k tokens), so the real
  ceiling is ~80–100 questions/day. A retry-storm once burned the daily
  quota — retries are now bounded. (`DECISIONS.md`.)
- **SQL access is direct and unsandboxed** against the DB. Acceptable because
  the data is read-only reference data and (in prod) Neon; a scoped
  read-only DB user would be required before pointing it at anything
  writable. The toolkit has no statement allow-list.
- **Multi-query expansion + RRF** sit only in front of the vector tool.
  Structured SQL questions never pay that extra LLM call. Tunable via
  `RAG_MULTIQUERY`, `RAG_SUBQUERIES`, `RAG_K_PER`, `RAG_K_RETURN`.
- **Conversation history is windowed and stateless.** The browser holds the
  transcript and sends a trailing window each call; the server never stores
  it. History is context-only — the agent is instructed never to re-answer
  an earlier turn and to ignore history that is not relevant.
- **Scope guardrails live in `SYSTEM_CONTEXT`**, reinforced by the
  `course-agent-qa` subagent (answer-quality regression) and the
  `course-app-redteam` subagent (prompt injection, SQLi, disclosure, abuse).

---

## 9. Deployment and configuration

Full runbook: `DEPLOYMENT.md`. Summary:

- **Host:** Render free web service, `render.yaml` in repo. `uvicorn app.api:app`.
- **Database:** Neon free Postgres. No expiry; compute autoscales to zero
  after ~5 min idle and wakes on the next query.
- **Vectors:** `pgvector` on the same Neon database — no add-on, no separate
  vector service.

### Environment variables

| Var | Where | Purpose |
|---|---|---|
| `DATABASE_URL` | Render + local scraper runs | Postgres connection string. **Its presence is what switches `db.py` to Postgres.** Absent → local SQLite at `data/courses.db`. |
| `GROQ_API_KEY` | Render (and local, for `agent.py`) | Preferred LLM provider. |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | optional | Fallback LLM providers, in that priority order after Groq. |
| `ENABLE_DOCS` | optional | Turns on `/docs`, `/redoc`, `/openapi.json`. Off in prod — information-disclosure surface. |
| `RAG_MULTIQUERY`, `RAG_SUBQUERIES`, `RAG_K_PER`, `RAG_K_RETURN` | optional | RAG expansion/retrieval tuning. Sensible defaults in code. |

`.env` in the project root is auto-loaded by `api.py`, `agent.py`, and the
data scripts. (A past bug: the `load_*` scripts did *not* load `.env`, so
"scrape straight to Neon" silently wrote to local SQLite instead — fixed.)

---

## 10. Operations / runbook

- **Monthly refresh:** locally, `python -m app.scraper --year YYYY --semester SEASON --subjects ...`
  with `DATABASE_URL` set to Neon. Check `GET /sync/status` first to see
  which departments users asked for. Do **not** use `--skip-recent` on a run
  meant to backfill a newly-added column.
- **Rolling the term window:** edit `CURRENT_YEAR` / `CURRENT_SEMESTER` in
  `app/terms.py` as terms advance, then re-run the history loaders.
- **403 from the scraper:** you are probably not on a residential IP, or
  going too fast/concurrent. Lower `--concurrency`, raise `--section-delay`.
  Cloud runners (GitHub Actions) do not fix this — same datacenter-IP block.
  Mitigation options for the monthly refresh are listed in `DEPLOYMENT.md`.
- **Embeddings after a bulk load or model change:** `python -m app.backfill_embeddings`.
- **First-time prod populate:** scrape locally to SQLite, verify, then
  `python -m app.migrate_sqlite_to_neon`.
- **Descriptions come back all NULL after a detailed scrape:** the catalog
  XML tag name differs from the candidates in `DESCRIPTION_TAGS` in
  `scraper.py`. Fetch one catalog URL by hand, read the real element name,
  one-line fix.
- **Regression-check the agent after touching `agent.py` / `SYSTEM_CONTEXT`
  / schema / `embeddings.py`:** run the `course-agent-qa` subagent (writes
  `qa_log.txt`). After touching `api.py` / guardrails / logging: run
  `course-app-redteam` (writes `security_findings.md`).

---

## 11. Security posture

- Baseline security headers on every response (CSP allowing only the page's
  own inline JS/CSS and Google Fonts, `X-Frame-Options: DENY`, HSTS,
  `nosniff`, `no-referrer`).
- Generic exception handler — driver/SQL/stack detail never reaches the
  client; the real error is logged server-side.
- `/ask` has a question-length cap and a per-IP rate limit.
- Interactive API docs and the OpenAPI schema are off unless `ENABLE_DOCS`.
- Prompt-injection / jailbreak resistance is a prompt-level guardrail in
  `SYSTEM_CONTEXT`, not a hard sandbox. The SQL tool can run arbitrary SELECT
  (and, technically, more) against the DB — mitigated by the data being
  read-only reference data, not by engine restrictions.
- Red-team history and fixes are in `security_findings.md` (first pass: 1
  high, 3 medium, 6 low — all fixed).

---

## 12. Conventions

- **One query text, both backends.** Write `?` placeholders and SQLite-ish
  syntax; never branch on backend outside `db.py`. Use `db.upsert()` instead
  of hand-writing `INSERT OR REPLACE` / `ON CONFLICT`.
- **`semester` / `term` values are lowercase; `subject` codes are uppercase.**
- **Decisions get logged.** Any architecture, data-source, or scope decision
  — including rejected alternatives and why they lost — gets a new dated
  section appended to `DECISIONS.md`. `implementation_plan.md` is *what*;
  `DECISIONS.md` is *why*.
- **The scraper is resumable and idempotent.** Rows upsert on their unique
  key; `Ctrl+C` is always safe.
- **No new standing cost, no new provider, no paid tier** without a logged
  decision.

---

## 13. Known limitations

- Cross-listed courses (CS 440 / ECE 448) are stored as separate rows per
  subject; not deduplicated.
- `enrollment_status` is a scrape-time snapshot, not live.
- No intra-run diff mode — a course is either fully re-fetched or (with
  `--skip-recent`) skipped entirely; no "only refresh enrollment" path.
- The SQL agent has unsandboxed DB access (see §11).
- Groq's ~200k-tokens/day cap means ~80–100 real questions/day in
  production.
- TRE → instructor join is name-only and imperfect.
- Gen Ed data is a single snapshot, not tracked per term.
- Vector search requires Postgres; a local SQLite setup has no RAG tool.

---

## 14. Roadmap / open threads

Tracked in `implementation_plan.md` and the tail of `DECISIONS.md`. Themes:
richer meeting/location querying, schedule-conflict tooling, wider historical
grade coverage, and continued UI iteration on the assistant-first layout.

---

## 15. Glossary

- **CRN** — Course Reference Number. Unique per section per term.
- **Detailed / fast mode** — the scraper with vs. without the per-section
  instructor/enrollment/meeting requests and the per-course description
  request.
- **Hybrid agent** — the answer engine combining a SQL toolkit (structured
  lookups) and a pgvector RAG tool (semantic/content questions).
- **Multi-query expansion** — one LLM call that rewrites a topic into several
  facets before the vector search, whose results are then merged with RRF.
- **RRF** — Reciprocal Rank Fusion; rank-based merge of several ranked result
  lists.
- **Hybrid sync** — the operating model: scrape locally from a residential
  IP, write to cloud Postgres; the Render app only ever reads Neon.
- **Active term window** — current term ±2, spring/summer/fall cycle; the
  span of history the loaders keep (`terms.py`).
- **TRE** — "Teachers Ranked as Excellent", a published UIUC list.
