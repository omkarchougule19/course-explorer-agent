# Deployment

Everything needed to stand this up on Render + Neon and keep it running.
Design rationale for the choices below lives in `DECISIONS.md`; this file is
the operational checklist.

---

## 1. Architecture in one picture

```
  Local machine (residential IP)            Cloud
  ┌───────────────────────────┐   scrape    ┌──────────────────────────┐
  │ app/scraper.py            │◀───XML──────│ UIUC Course Explorer API │
  │ app/sync_requests.py      │             └──────────────────────────┘
  │ app/migrate_sqlite_to_neon│
  │ app/backfill_embeddings.py │──writes──┐
  └───────────────────────────┘          ▼
                                 ┌──────────────────┐   reads   ┌──────────────────┐
                                 │  Neon Postgres   │◀──────────│ Render web service│
                                 │  + pgvector      │           │ FastAPI (app.api) │
                                 └──────────────────┘           └──────────────────┘
```

- **Render only ever reads** from Neon. It never scrapes anything (its
  datacenter IP is hard-blocked by UIUC's WAF).
- **All writes to Neon happen from your local machine**, on a residential IP:
  the initial load (a SQLite→Neon migration), embedding backfills, and
  demand-driven per-department refreshes.
- Neon compute scales to zero when idle and wakes on the next query. Data is
  never deleted.

---

## 2. Prerequisites

| Thing | Where | Cost |
|---|---|---|
| Neon project (Postgres 16+, `pgvector` available) | neon.tech | free tier, no expiry |
| Groq API key | console.groq.com | free tier (200,000 tokens/day) |
| Render account | render.com | free web service |
| This repo on GitHub | — | — |
| Local Python env with `requirements.txt` installed | your machine | — |

`pgvector` does **not** need to be pre-enabled in Neon — the app runs
`CREATE EXTENSION IF NOT EXISTS vector` itself on first connect.

---

## 3. First-time setup

### 3.1 Neon

1. Create a project. Copy the **connection string** (the `postgres://user:pass@host/db?sslmode=require` form).
2. Put it in your local `.env` as `DATABASE_URL=...` (this file is gitignored).

### 3.2 Load data into Neon (from your local machine)

The local `data/courses.db` is the source of truth for the initial load —
scraping straight to Neon does not work (WAF soft-block, see `DECISIONS.md`).

```bash
# with DATABASE_URL set in .env:
python -m app.migrate_sqlite_to_neon      # sections, meetings, gen_ed_categories, empty grade tables
python -m app.backfill_embeddings         # course_embeddings (pgvector) - a few thousand rows, run in
                                          # chunks with --limit N if a single run gets killed
```

Sanity check:

```bash
python -m app.sync_requests --list        # should list ~187 departments with a recent "last synced"
```

### 3.3 Render

1. New → **Blueprint**, point it at this repo. `render.yaml` defines the
   service (Python, free plan, `uvicorn app.api:app`, health check `/api`).
2. Build step pre-downloads the embedding model into `./model_cache` so the
   first request isn't a 30–90 s cold download.
3. After the first deploy, set environment variables in the Render dashboard
   (all are `sync: false` in `render.yaml`, i.e. Render won't invent them):

| Var | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | **yes** | Neon connection string |
| `GROQ_API_KEY` | **yes** (for `/ask`) | LLM for the assistant |
| `GEMINI_API_KEY` | no | fallback LLM, used only if `GROQ_API_KEY` is unset |
| `OPENAI_API_KEY` | no | second fallback |
| `ADMIN_TOKEN` | no | enables `GET /admin/ask-log`; endpoint 404s while unset |
| `ASK_RATE_PER_HOUR` | no (default 10) | per-IP assistant question cap / hour |
| `ASK_RATE_PER_DAY` | no (default 40) | per-IP assistant question cap / day |
| `ASK_MAX_CHARS` | no (default 500) | reject questions longer than this |

### 3.4 Post-deploy checks

```bash
curl https://<your-app>.onrender.com/api        # {"service": ...}
curl https://<your-app>.onrender.com/stats      # section/subject/course counts > 0
curl -XPOST https://<your-app>.onrender.com/ask \
     -H 'Content-Type: application/json' \
     -d '{"question":"who teaches CS 225"}'      # a real answer, or a clean "over daily limit" message
```

Open the site: the header, Browse Sections (Term / Subject / Level filters),
Ask the Agent, and Department Data panels should all populate.

---

## 4. Ongoing operations

### 4.1 There is no scheduled full re-scrape

By design. The WAF soft-blocks a full catalog sweep after a handful of
departments. Freshness is **demand-driven** instead.

### 4.2 Processing department refresh requests

Users click **Sync** on a stale department in the UI; that increments a
counter in `sync_requests`. You process the queue locally, on your
residential IP:

```bash
python -m app.sync_requests --list                # departments ranked by pending requests
python -m app.sync_requests --run PHYS ECE MATH    # refresh these, in order
python -m app.sync_requests --run --top 5          # refresh the 5 most-requested
```

- Scrapes the current term always, plus the next term **if UIUC has published
  it**.
- Skips courses synced within the last 7 days (the UI also hides the Sync
  button for those).
- Stops after 2 consecutive departments come back empty despite having data
  on file — that's the WAF soft-rejecting. Wait a while and re-run; it
  resumes where it stopped.
- Re-embeds each refreshed course's description as it goes, so no separate
  backfill is needed after a `--run`.

### 4.3 After a large manual re-scrape

If you ever do a big `python -m app.scraper --subjects A,B,C ...` run against
Neon, follow it with:

```bash
python -m app.backfill_embeddings                  # fills any course_embeddings gaps
```

### 4.4 Rolling the term forward

When registration moves to the next term, update **two** places:

- `app/terms.py` — `CURRENT_YEAR` / `CURRENT_SEMESTER`
- `static/index.html` — the `CURRENT_TERM` constant (used to preselect the
  Term dropdown)

### 4.5 Grade / teaching-ranking data

`grade_distributions` and `teachers_ranked_excellent` are empty until UIUC's
upstream sources publish for the term. Refill them locally when they do:

```bash
python -m app.load_grades
python -m app.load_tre
```

---

## 5. Guardrails & activity log

### 5.1 What's enforced on `/ask`

Every call is checked **before** the LLM runs:

1. **Length cap** — questions over `ASK_MAX_CHARS` (500) are rejected (`422`).
2. **Per-IP rate limit** — `ASK_RATE_PER_HOUR` (10) and `ASK_RATE_PER_DAY`
   (40) LLM-spending questions per client IP. Over the limit → `429` with a
   message that points the user at the browse tools. Only `answered` and
   `refused` calls count; provider errors don't, so a Groq outage never locks
   anyone out.
3. **Scope guardrail** — `SYSTEM_CONTEXT` in `app/agent.py` tells the model
   to refuse anything not answerable from the catalog tables, and to ignore
   "ignore your instructions"-style injection. Refusals are tagged `refused`
   in the log.

The client IP is read from `X-Forwarded-For` (Render sets it). It's
spoofable, so treat the rate limit as friction, not security.

### 5.2 The `ask_log` table

Every attempt is written to `ask_log`:

| column | note |
|---|---|
| `ts` | UTC ISO timestamp |
| `client_ip` | first hop of X-Forwarded-For |
| `question` | first 1000 chars |
| `outcome` | `answered` / `refused` / `rate_limited` / `too_long` / `error` |
| `answer_preview` | first 500 chars of the answer |
| `latency_ms` | agent round-trip |

### 5.3 Reading the log

Set `ADMIN_TOKEN`, then:

```bash
curl "https://<your-app>.onrender.com/admin/ask-log?token=$ADMIN_TOKEN&limit=100"
curl "https://<your-app>.onrender.com/admin/ask-log?token=$ADMIN_TOKEN&outcome=refused"
curl "https://<your-app>.onrender.com/admin/ask-log?token=$ADMIN_TOKEN&ip=1.2.3.4"
```

Or straight from Neon:

```sql
SELECT ts, client_ip, outcome, question
FROM ask_log
ORDER BY id DESC
LIMIT 100;

-- what people ask that gets refused - candidates for a sharper SYSTEM_CONTEXT rule
SELECT question, COUNT(*) FROM ask_log WHERE outcome = 'refused'
GROUP BY question ORDER BY 2 DESC;

-- who is hammering it
SELECT client_ip, COUNT(*) FROM ask_log
WHERE ts >= (now() - interval '1 day')::text
GROUP BY client_ip ORDER BY 2 DESC;
```

### 5.4 Tightening the scope guardrail

If the log shows a recurring class of junk the model still answers, add an
explicit line to `SYSTEM_CONTEXT` in `app/agent.py` (there's already a
scope-boundary block near the top), redeploy, and re-check with the
`course-agent-qa` subagent.

---

## 6. Limits & costs

| Resource | Free-tier limit | Practical ceiling |
|---|---|---|
| Groq `openai/gpt-oss-120b` | 200,000 tokens/day | ~80–100 assistant questions/day (full schema sent each call) |
| Neon | no time limit; compute sleeps when idle | fine for low traffic; first query after idle wakes it (~1 s) |
| Render free web | sleeps after 15 min idle; slow cold start | acceptable for a low-traffic tool |

When the Groq daily budget is exhausted, `/ask` returns a plain "the
provider's rate limit was hit" message (tagged `error`, not counted against
users). Browse and Department Data are unaffected — they never call the LLM.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `scraper.py` / `sync_requests --run` reports "no courses found" for many departments in a row | WAF soft-reject. Not a bug. Wait, re-run. See `DECISIONS.md` "Path B" and the 403 mitigation notes. |
| First request after deploy or after idle is very slow | Render cold start + embedding model load. Subsequent requests are fast. |
| `/ask` says "No LLM API key found" | `GROQ_API_KEY` not set in the Render env. |
| `/ask` always says the provider rate limit was hit | Groq's 200K tokens/day is spent. Resets daily. |
| `course_content_search` never fires / semantic questions give SQL-only answers | `course_embeddings` is empty on Neon — run `python -m app.backfill_embeddings` locally. |
| `grade_distributions` / `teachers_ranked_excellent` queries return nothing | Upstream hasn't published for the term. Expected. |
| `/admin/ask-log` returns 404 | `ADMIN_TOKEN` isn't set. Set it and redeploy. |
