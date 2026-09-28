# Deployment (v2)

Everything needed to stand Illini Course Copilot up on Render + Neon and keep
it running, written from the code as of 2026-09-24. The reasoning behind the
choices below is in `DECISIONS_v2.md`; this file is the operational checklist.
Section numbers are stable: code comments refer to them (e.g. §3.5).

---

## 1. Architecture in one picture

```
  Your machine (residential IP)                Cloud
  ┌─────────────────────────────┐   scrape    ┌──────────────────────────┐
  │ app/scraper.py              │◀────XML─────│ UIUC Course Explorer     │
  │ app/sync_requests.py        │             └──────────────────────────┘
  │ app/load_*.py               │
  │ app/backfill_embeddings.py  │── writes the catalog ──┐
  └─────────────────────────────┘                        ▼
                                  ┌───────────────────────┐        ┌──────────────────────┐
                                  │ Neon Postgres         │◀───────│ Render web service   │
                                  │ + pgvector            │  reads │ FastAPI (app.api)    │
                                  │                       │ ─ ─ ─ ─│ writes only its own  │
                                  └───────────────────────┘        │ log/feedback tables  │
                                                                   └──────────────────────┘
```

- **Catalog data is written only from your machine**, on a residential IP:
  scrapes, department refreshes, the loaders, embedding backfills and the
  one-time migration. Render never scrapes: UIUC's firewall rejects its
  datacenter IP.
- **The web app writes only its own bookkeeping**: `ask_log`,
  `answer_feedback`, `site_feedback` and `sync_requests` (and creates those
  tables at startup if they're missing).
- **SQL written by the model** runs on a separate SELECT-only role (§3.5).
- Neon compute scales to zero when idle and wakes on the next query. Data is
  never deleted.

---

## 2. Prerequisites

| Thing | Where | Cost |
|---|---|---|
| Neon project (Postgres 16+, `pgvector` available) | neon.tech | free tier, no expiry |
| Groq API key | console.groq.com | free tier (about 200,000 tokens/day per model) |
| Render account | render.com | free web service |
| This repo on GitHub | — | — |
| Local Python env with `requirements.txt` installed (a hash-pinned lock built for Python 3.14, the version Render uses) | your machine | — |

`pgvector` doesn't need to be enabled by hand: `CREATE EXTENSION IF NOT
EXISTS vector` runs the first time the scraper, the embeddings backfill or the
migration touches `course_embeddings`.

---

## 3. First-time setup

### 3.1 Neon

1. Create a project. Copy the **connection string**
   (`postgres://user:pass@host/db?sslmode=require`).
2. Put it in your local `.env` as `DATABASE_URL=...` (`.env` is gitignored).
   Every script loads `.env` itself.

### 3.2 Load data into Neon (from your machine)

Scraping a whole term straight to Neon doesn't work: the firewall soft-blocks
a long sweep after a few departments. The initial load copies a complete local
`data/courses.db` instead.

```bash
# with DATABASE_URL set in .env:
python -m app.migrate_sqlite_to_neon   # sections, meetings, gen_ed_categories,
                                       # grade_distributions, teachers_ranked_excellent
python -m app.load_prereqs             # prerequisites (parsed from descriptions)
python -m app.load_calendar --term 2026-fall \
    --url https://registrar.illinois.edu/fall-2026-academic-calendar/
python -m app.backfill_embeddings      # course_embeddings; use --limit N to run in chunks
```

Sanity check:

```bash
python -m app.sync_requests --list     # lists every department (191 today) with its last sync
```

### 3.3 Render

1. **New → Blueprint**, pointed at this repo. `render.yaml` defines the
   service (Python, free plan, `uvicorn app.api:app`, health check `/api`).
   The build installs `requirements.txt` and pre-downloads the embedding model
   into `./model_cache`, so the first semantic question isn't a 30-90 s
   download.
   *The current production service was created by hand, not from the
   blueprint: it's named `course-explorer-agent`
   (`course-explorer-agent.onrender.com`), while `render.yaml` says
   `illini-course-copilot`. Keep them in mind as separate until one is renamed.*
2. **Deploys are manual.** Pushing to `master` doesn't deploy. In the Render
   dashboard: **Manual Deploy → Deploy latest commit**. Changing an
   environment variable and saving also triggers a rebuild of the current
   commit.
3. Set the environment variables in the Render dashboard (all are
   `sync: false` in `render.yaml`, so Render won't invent them):

| Var | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | **yes** | Neon connection string (owner role). |
| `DATABASE_URL_RO` | **yes** | The SELECT-only `app_ro` role for model-written SQL (§3.5). If it's missing on Render, the assistant answers "Can't answer that right now" rather than run model SQL as the owner. |
| `GROQ_API_KEY` | **yes** (for `/ask`) | The assistant's LLM. |
| `OPENAI_API_KEY` | no | Fallback provider, used only when `GROQ_API_KEY` is unset (costs money). |
| `ADMIN_TOKEN` | no | Enables the admin dashboard and every `/admin/*` route; without it they always return 403. |
| `ASK_MAX_CONCURRENT` | no (code default 4; **2 in production**) | Questions answered at once; past it, an immediate 503 "busy". |
| `ASK_GLOBAL_PER_DAY` | no (default 250) | **Shared** daily cap across all clients: the real protection for the Groq budget. |
| `ASK_RATE_PER_HOUR` / `ASK_RATE_PER_DAY` | no (10 / 60) | Per-IP caps (friction only: the IP comes from a spoofable header). |
| `ASK_MAX_CHARS` | no (500) | Longest accepted question. |
| `MAX_BODY_BYTES` | no (262144 = 256 KB) | Largest accepted request body; 413 above it. |
| `ANSWER_FEEDBACK_PER_IP_DAY` / `ANSWER_FEEDBACK_GLOBAL_DAY` | no (50 / 2000) | New 👍/👎 rows per IP and in total per day (re-votes always allowed). |
| `SITE_FEEDBACK_MAX_CHARS` / `SITE_FEEDBACK_PER_IP_DAY` | no (2000 / 5) | Feedback-box length and per-IP daily cap. |
| `SQL_STATEMENT_TIMEOUT_MS` | no (8000) | Per-statement timeout for model-written SQL. |
| `ALLOW_RW_AGENT_DB` | no | Temporary override: lets the assistant run without `DATABASE_URL_RO` on Render. The SQL guard still applies. |
| `LLM_PROVIDER` | no | Force `groq` or `openai`. Unset = auto-detect, Groq first. Its own key must be set. |
| `GROQ_MODEL` | no (`openai/gpt-oss-120b`) | Groq model. The daily cap is per model, so another model has its own budget on the same key. `OPENAI_MODEL` (`gpt-4o-mini`) does the same. |
| `GROQ_FALLBACK_MODEL` | no (`qwen/qwen3.8-27b`) | Retried once after a Groq 429 on the primary; `off` disables. |
| `MAX_QUERY_RESULT_CHARS` | no (6000) | A single SQL result sent back to the model is cut here, with a note to narrow the query. |
| `RAG_MULTIQUERY` / `RAG_SUBQUERIES` / `RAG_K_PER` / `RAG_K_RETURN` | no (on / 3 / 6 / 10) | Multi-query expansion and Reciprocal Rank Fusion for semantic search. `RAG_MULTIQUERY=0` falls back to a single query. |
| `ANSWER_CITATIONS` | no (on) | The "Sources: …" footer on answers. `0` disables it. |
| `ENABLE_DOCS` | no | Any value exposes `/docs`, `/redoc`, `/openapi.json` (off by default). |
| `SQL_PIPELINE` / `SQL_PIPELINE_INTENT_CHECK` / `SQL_PIPELINE_TERSE_SCHEMA` | no (off / `repair` / off) | The opt-in Generator → Critic → Repair pipeline. Unset in production. |

### 3.4 Post-deploy checks

```bash
B=https://<your-app>.onrender.com
curl $B/api                          # {"service": "Illini Course Copilot", ...}
curl $B/stats                        # section / subject / course counts > 0
curl -s -D - -o /dev/null $B/ | grep -i content-security-policy   # script-src 'self'
curl -XPOST $B/ask -H 'Content-Type: application/json' \
     -d '{"question":"who teaches CS 225 in fall 2026"}'   # a real, cited answer
```

Then open the site: Home (the assistant and Browse Sections with its filters),
Departments, Calendar, Schedule, Data Freshness and How It Works should all
load and populate.

### 3.5 Read-only role for the assistant

The `/ask` agent (and the opt-in `SQL_PIPELINE`) writes and runs SQL. Three
independent layers keep that SQL read-only and scoped:

1. the prompt (`SYSTEM_CONTEXT`);
2. `app/sql_guard.py`: every statement is parsed with sqlglot before it runs,
   and anything but a single SELECT over the catalog tables is rejected. That
   covers writes and DDL, data-modifying CTEs, `SELECT INTO`, `FOR UPDATE`, the
   `ask_log` / feedback tables, `pg_catalog`, and functions like `pg_sleep`.
   It also runs every statement in a `READ ONLY` transaction with a
   `statement_timeout` (`SQL_STATEMENT_TIMEOUT_MS`, default 8000);
3. a database role that can only read the catalog tables (this section).

In the Neon SQL editor (or `psql`), as the owner role, against your database:

```sql
CREATE ROLE app_ro LOGIN PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE neondb TO app_ro;      -- your db name
GRANT USAGE ON SCHEMA public TO app_ro;
-- Only the tables the assistant may query (app/agent.py INCLUDED_TABLES).
-- Deliberately NOT ask_log / answer_feedback / site_feedback / sync_requests:
-- those hold other users' IPs and questions.
GRANT SELECT ON sections, meetings, grade_distributions,
    teachers_ranked_excellent, gen_ed_categories, prerequisites,
    academic_calendar TO app_ro;
ALTER ROLE app_ro SET default_transaction_read_only = on;
ALTER ROLE app_ro SET statement_timeout = '8s';
```

**If `app_ro` was created with an earlier recipe** (`GRANT SELECT ON ALL
TABLES` + `ALTER DEFAULT PRIVILEGES`), undo that blanket grant first, then run
the `GRANT SELECT ON sections, ...` and `ALTER ROLE` lines above:

```sql
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM app_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_ro;
```

Check: connected as `app_ro`, `SELECT 1 FROM ask_log LIMIT 1` must fail with
"permission denied".

Then set `DATABASE_URL_RO` in Render to that role's connection string (same
host and database as `DATABASE_URL`, different user and password). Generate
the password with something like
`python -c "import secrets; print(secrets.token_urlsafe(24))"`, and check the
pasted URL ends in `...sslmode=require&channel_binding=require` with nothing
stuck to it (a stray suffix once broke the connection). The app uses this role
only for model-written SQL; every write path and the semantic-search tool keep
using `DATABASE_URL`.

**On Render, `DATABASE_URL_RO` is required** (the `RENDER` variable Render
sets is how the app knows). `ALLOW_RW_AGENT_DB=1` bypasses the requirement
temporarily; layers 1 and 2 still apply. Locally, an unset `DATABASE_URL_RO`
falls back to `DATABASE_URL`, and SQLite mode never touches Postgres.

---

## 4. Ongoing operations

### 4.1 There is no scheduled full re-scrape

By design. The firewall soft-blocks a full-catalog sweep after a handful of
departments. Freshness is **demand-driven** instead.

### 4.2 Processing department refresh requests

Visitors press **Sync** on a stale department (Departments page); that
increments a counter in `sync_requests`. Process the queue locally, on your
residential IP, with `DATABASE_URL` pointing at Neon:

```bash
python -m app.sync_requests --list                 # departments ranked by pending requests
python -m app.sync_requests --run PHYS ECE MATH    # refresh these, in order
python -m app.sync_requests --run --top 5          # refresh the 5 most-requested
```

- Scrapes the current term always, plus the next term **if UIUC has published
  it**.
- Skips departments synced within the last 7 days (the page also hides their
  Sync button).
- Stops after 2 departments in a row come back empty despite having data on
  file, or have their term probe rejected: that's the firewall wall. Wait and
  re-run; it resumes where it stopped.
- Embeds each refreshed course's description as it goes, so no separate
  backfill is needed after a `--run`.
- **Open work:** the fall 2026 catch-up stopped at the wall with 84 of 191
  subjects loaded (plan item 7).

### 4.3 After a large manual re-scrape

After a big `python -m app.scraper --year ... --semester ... --subjects ...`
run against Neon:

```bash
python -m app.backfill_embeddings   # fills any course_embeddings gaps
python -m app.load_prereqs          # re-derive prerequisites from the new descriptions
```

### 4.4 Rolling the term forward

When registration moves to the next term, update **four** places:

- `app/terms.py`: `CURRENT_YEAR` / `CURRENT_SEMESTER`
- `static/index-page.js`: `CURRENT_TERM` (preselects the Browse term)
- `static/departments-page.js`: `DC_CURRENT_TERM`
- `static/calendar-page.js`: `CURRENT_TERM`

Then load the new term's calendar (§4.6) and deploy. The assistant's prompt
needs no edit: its DATA NOTES (the terms in the data, the latest term, which
terms have unpublished registration) are read from the database at startup.

### 4.5 Grade / teaching-ranking data

`grade_distributions` and `teachers_ranked_excellent` are empty until the
upstream datasets publish terms inside the window. Refill them locally when
they do:

```bash
python -m app.load_grades
python -m app.load_tre
```

The next deploy (or restart) updates the prompt's note that they're empty.

### 4.6 Prerequisites and the academic calendar

`prerequisites` is re-derived from `sections.description`; re-run it after any
scrape or `load_catalog_snapshot` that changes descriptions:

```bash
python -m app.load_prereqs
```

`academic_calendar` is loaded per term from the registrar. The per-term URL
isn't derivable for every term, so pass it (find the page from
`https://registrar.illinois.edu/academic-calendars/`):

```bash
python -m app.load_calendar --term 2026-fall \
    --url https://registrar.illinois.edu/fall-2026-academic-calendar/
# or, if the page won't fetch from your network:
python -m app.load_calendar --term 2026-fall --file saved_page.html
```

Both loaders create their own table and are safe to re-run. Every read path
(the assistant, `GET /calendar`, `GET /courses/{s}/{n}/prereqs`) says "not
available yet" when a table is absent, so there's no ordering dependency with
a deploy.

### 4.7 Changing the assistant

Before deploying a change to `app/agent.py` or the prompt: run the offline
tests (`python -m evals.test_agent_guards` and the others listed in
`docs/REFERENCE_v2.md`), then the eval on the live database with OpenAI, so
Groq's shared daily budget stays with users:

```bash
python -m evals.run --arms prod --db env --provider openai -y
```

Compare against a second run of the old version (single runs vary), then try a
few targeted questions on Groq.

---

## 5. Guardrails & activity log

### 5.1 What's enforced on `/ask` (and `/ask/stream`)

The site calls `POST /ask/stream` (Server-Sent Events: live "Running SQL…"
status, then the answer token by token, then its sources). `POST /ask` is the
non-streaming JSON fallback. Both run the same checks, in this order, before
any model call; a blocked stream request gets a normal JSON error.

1. **Body size**: over `MAX_BODY_BYTES` (256 KB) → `413`.
2. **Concurrency**: more than `ASK_MAX_CONCURRENT` questions in flight (2 in
   production) → an immediate `503` "busy, try again in a few seconds".
3. **Length**: over `ASK_MAX_CHARS` (500) → `422`.
4. **Reserved slot**: a `pending` row is written to `ask_log`.
5. **Shared daily cap**: `ASK_GLOBAL_PER_DAY` (250) across all clients in the
   trailing 24 h → `429`. Keys on nothing the client controls.
6. **Per-IP limits**: `ASK_RATE_PER_HOUR` (10) and `ASK_RATE_PER_DAY` (60) →
   `429`. Friction only: the IP is the first `X-Forwarded-For` hop, which a
   caller can forge.

Steps 5 and 6 count `answered`, `refused` and `pending` rows **in reservation
order**, so a burst of parallel requests can't all pass on the same count.
Provider errors don't count, so a Groq outage never locks anyone out. After
the answer, the reserved row is rewritten with the real outcome and latency.

**Scope and safety** come from the prompt (refuses out-of-scope questions and
role-change attempts with one fixed sentence, so they're tagged `refused`) and
from the SQL layers in §3.5.

### 5.2 The `ask_log` table

| column | note |
|---|---|
| `ts` | UTC ISO timestamp (text) |
| `client_ip` | first hop of `X-Forwarded-For` |
| `question` | first 1,000 chars |
| `outcome` | `answered` / `refused` / `rate_limited` / `global_limited` / `too_long` / `error` / `pending` (in flight, or a request that died mid-way) |
| `answer_preview` | first 500 chars of the answer |
| `latency_ms` | agent round trip |

`GET /ask/summary` is **public** (no token) and returns only aggregates:
`unique_7d` (distinct client IPs in 7 days, for the "People Asking" tile),
`global_calls_24h` and `global_limit` (for the usage bar under the
assistant). No IPs or question text.

### 5.2b The `answer_feedback` table

The 👍/👎 under each answer writes here (`app/feedback.py`). One row per vote;
re-voting the same answer replaces the prior row (de-duped on `client_ip` +
`question` + `answer`) and is always allowed.

| column | note |
|---|---|
| `ts` | UTC ISO timestamp |
| `client_ip` | first hop of `X-Forwarded-For` |
| `vote` | `up` or `down` |
| `question` / `answer` | browser-supplied, capped at 1,000 / 4,000 chars |
| `history_json` | JSON `[{q, a}, …]` snapshot of the chat, **downvotes only** |
| `reviewed_at` | `NULL` until an operator triages that downvote |

New rows are capped at `ANSWER_FEEDBACK_PER_IP_DAY` (50) per IP and
`ANSWER_FEEDBACK_GLOBAL_DAY` (2,000) in total per 24 h. `POST /ask/feedback`
never fails loudly: a refused or failed write returns `{"ok": false}` with
HTTP 200.

**Biweekly:** open `/admin.html` → *Downvotes — needs review*, read each
transcript, fix the cause (prompt, data, loaders), then **Mark reviewed**.

### 5.2c The `site_feedback` table

The free-text box in the page footer writes here (`app/site_feedback.py`).

| column | note |
|---|---|
| `ts` | UTC ISO timestamp |
| `client_ip` | first hop of `X-Forwarded-For` |
| `message` | capped at `SITE_FEEDBACK_MAX_CHARS` (2,000) |
| `page` | the path it was sent from, clipped to 300 chars |
| `reviewed_at` | `NULL` until an operator marks it handled |

`POST /feedback` returns `{"ok": <bool>, "reason": <str>}` and never 5xx;
`reason` is `ok` / `empty` / `too_long` / `rate_limited` / `error`. Capped at
`SITE_FEEDBACK_PER_IP_DAY` (5) per IP per day.

### 5.3 Reading the log: dashboard and endpoints

The dashboard is at `https://<your-app>.onrender.com/admin.html`. The page is
public; every panel's data needs `ADMIN_TOKEN`, entered once, kept in that
tab's `sessionStorage` and sent as the `X-Admin-Token` header. "Lock" clears
it.

Panels: usage tiles (unique clients 24 h / 7 d / all time, question volume,
downvotes and site feedback to review), outcome breakdown, a 30-day activity
chart, a per-client-IP rollup, the question log with filters, and both review
queues.

Every admin route requires the header and always answers `403` on any failure
(unset, missing or wrong token), so the response never reveals whether a token
is configured. A `?token=` query parameter is ignored.

| route | purpose |
|---|---|
| `GET /admin/ask-log?outcome=&ip=&limit=` | recent `/ask` rows |
| `GET /admin/ask-stats` | tile aggregates, outcome breakdown, feedback tallies |
| `GET /admin/clients?limit=` | per-client-IP rollup |
| `GET /admin/activity?days=` | questions and unique clients per UTC day |
| `GET /admin/feedback?vote=&reviewed=&limit=` | vote rows; `vote=down&reviewed=0` is the triage queue |
| `POST /admin/feedback/{id}/reviewed` | mark one downvote reviewed |
| `GET /admin/site-feedback?reviewed=&limit=` | feedback-box rows; `reviewed=0` is the triage queue |
| `POST /admin/site-feedback/{id}/reviewed` | mark one feedback row reviewed |

```bash
H="X-Admin-Token: $ADMIN_TOKEN"
curl -H "$H" "https://<your-app>.onrender.com/admin/ask-log?limit=100"
curl -H "$H" "https://<your-app>.onrender.com/admin/ask-log?outcome=refused"
curl -H "$H" "https://<your-app>.onrender.com/admin/ask-stats"
curl -H "$H" "https://<your-app>.onrender.com/admin/feedback?vote=down&reviewed=0"
curl -H "$H" "https://<your-app>.onrender.com/admin/site-feedback?reviewed=0"
```

Or straight from Neon (`ts` is ISO text such as `2026-09-24T07:04:20+00:00`,
so compare it with an ISO string):

```sql
SELECT ts, client_ip, outcome, question
FROM ask_log ORDER BY id DESC LIMIT 100;

-- what gets refused: candidates for a sharper prompt rule
SELECT question, COUNT(*) FROM ask_log WHERE outcome = 'refused'
GROUP BY question ORDER BY 2 DESC;

-- who is hammering it (last 24 h)
SELECT client_ip, COUNT(*) FROM ask_log
WHERE ts >= to_char((now() - interval '1 day') AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
GROUP BY client_ip ORDER BY 2 DESC;

-- requests that died mid-way (still pending after an hour)
SELECT id, ts, question FROM ask_log
WHERE outcome = 'pending'
  AND ts < to_char((now() - interval '1 hour') AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"+00:00"');
```

### 5.4 Tightening the scope guardrail

If the log shows a recurring kind of question the assistant handles badly,
add a rule to `SYSTEM_CONTEXT` in `app/agent.py` in the section it belongs to
(rules that shape the SQL go under HOW TO QUERY, not HOW TO ANSWER, or the
model won't apply them while writing the query), measure it (§4.7), deploy,
and re-check with the `course-agent-qa` subagent.

---

## 6. Limits & costs

| Resource | Free-tier limit | Practical ceiling |
|---|---|---|
| Groq `openai/gpt-oss-120b` | about 200,000 tokens/day and 8,000 tokens/minute per model | roughly 80-100 questions/day; busy moments are slow (the prompt is about 2,200 tokens and is re-sent each step) |
| Groq `qwen/qwen3.8-27b` (fallback) | its own daily budget; about 1,000 output tokens/minute | a low-traffic safety net |
| Neon | no time limit; compute sleeps when idle | fine for low traffic; the first query after idle wakes it |
| Render free web service | sleeps after about 15 min idle; 512 MB | cold start on the first visit after idle, plus about 2 s of app startup |

The Groq key is shared by production, development and evals, so an eval run
spends the budget students need; run full evals on OpenAI (§4.7). When the
daily budget is spent, `/ask` answers that the provider's rate limit was hit
(tagged `error`, not counted against users). Browsing, Departments, the
Calendar and the Schedule never call the LLM.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `sync_requests --run` / `scraper` finds "no courses" for many departments in a row | The firewall soft-reject (the wall). Not a bug. Wait and re-run. |
| `python app/scraper.py ...` fails with `ModuleNotFoundError: No module named 'app'` | Run scripts as modules from the project root: `python -m app.scraper ...`. |
| First request after a deploy or idle spell is very slow | Render cold start, plus the app's startup (embedding model load, table checks). The assistant itself is built in a background thread at startup; if the logs show `[startup] agent warm-up skipped`, the first question builds it instead (the logged reason says why, e.g. `DATABASE_URL_RO` missing). Later requests are fast. |
| `/ask` says "Can't answer that right now. The assistant isn't available" | A setup problem, logged server-side with the real reason: most often `DATABASE_URL_RO` missing on Render (§3.5), a missing LLM key, or the database unreachable. Check the Render logs for `[agent] setup failed`. |
| `/ask` returns 503 "busy" | `ASK_MAX_CONCURRENT` questions are already in flight. Expected under load; retry. |
| `/ask` returns 413 | The request body is over `MAX_BODY_BYTES`. The site never sends that much. |
| `/ask` says the provider's rate limit was hit | Groq's daily budget for the model (and the fallback) is spent. Resets daily. |
| Semantic questions ("courses about X") give SQL-only answers | `course_embeddings` is empty on Neon: run `python -m app.backfill_embeddings` locally. |
| Grade or ranking questions return "There's no data for that in this dataset yet" | Upstream hasn't published terms in the window. Expected (§4.5). |
| Model-written SQL errors with "permission denied" or "read-only transaction" | Working as intended: the SQL layers blocked it. If a normal question hits this, check `app_ro`'s grants (§3.5). |
| `/admin/*` always returns 403 | `ADMIN_TOKEN` isn't set on the server, or the `X-Admin-Token` header doesn't match (a `?token=` parameter is ignored). |
| `/admin.html` panels say "Couldn't load" | The page is public; the data needs the token. Enter it in the gate. Right after idle, retry once Neon and Render wake. |
| `/docs` returns 404 | Expected: set `ENABLE_DOCS` to turn it on. |
| A deploy doesn't seem to change anything | Deploys are manual: after pushing, use Manual Deploy in the Render dashboard. |
