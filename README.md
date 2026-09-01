# UIUC Course Explorer Data Agent

A small dataset and agent tool built on top of UIUC's public Course Explorer
API. Scrapes course, section, instructor, and enrollment data across
semesters, exposes it through a FastAPI backend with a browser UI, and
answers plain English questions about it through a LangChain agent that
writes and runs the SQL itself.

## Why this exists

Built as a work sample. It touches the same core loop as most dataset
management work: pull data from an external source, structure it into a
documented schema, expose it through an API, and let someone ask a question
in plain language instead of writing SQL by hand.

## Data source

UIUC's Course Explorer publishes a public, unauthenticated XML API:
https://courses.illinois.edu/cisdocs/explorer

```
/cisapp/explorer/schedule/{year}/{semester}.xml                       -> subjects
/cisapp/explorer/schedule/{year}/{semester}/{subject}.xml             -> courses
/cisapp/explorer/schedule/{year}/{semester}/{subject}/{course}.xml    -> sections (id + name only)
/cisapp/explorer/schedule/{year}/{semester}/{subject}/{course}/{crn}.xml -> full section detail
                                                                           (instructor, enrollment status)
/cisapp/explorer/catalog/{year}/{semester}/{subject}/{course}.xml     -> course catalog description
```

The schedule course level endpoint only lists section IDs and names.
Instructor and live enrollment status live one level deeper, at the per
section endpoint. The course description lives in a separate catalog module
entirely. A full "detailed" scrape therefore costs two extra requests per
course beyond the base listing: one for the description (once per course,
not per section) and one per section for instructor/enrollment. The scraper
supports `--fast` to skip all of that when you only need the course and
section list quickly.

## Setup

```bash
cd course-explorer-agent
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 1. Scrape data

```bash
# A few departments, fast mode (no instructor/enrollment, quick sanity check)
python app/scraper.py --year 2026 --semester fall --subjects CS,STAT,IS --fast

# Same departments, full detail (instructor + enrollment status)
python app/scraper.py --year 2026 --semester fall --subjects CS,STAT,IS

# Every subject offered that term. Courses are fetched concurrently
# (10 workers by default), which is what makes a full-catalog scrape
# practical instead of taking hours.
python app/scraper.py --year 2026 --semester fall

# Tune concurrency up/down. Lower it if you start seeing rate-limit warnings.
python app/scraper.py --year 2026 --semester fall --concurrency 20

# Re-running for the same term? Skip courses already scraped in the last
# 24 hours instead of re-fetching everything.
python app/scraper.py --year 2026 --semester fall --skip-recent 24
```

This writes to `data/courses.db` (SQLite). Re-running is safe, rows are
upserted on `(year, semester, subject, course_number, crn)`. `Ctrl+C` at any
point is safe too — whatever's been scraped so far is already committed, and
the run is resumable.

Already have a database from before course descriptions were added? No need
to delete it — the schema migrates automatically (`description` column gets
added on the next run). Just re-run the scraper for that term in detailed
mode to backfill descriptions into existing rows; **don't** use
`--skip-recent` for that one run, since it doesn't know descriptions are
missing and would skip courses that look "already scraped."

Flags:
- `--fast` — skip per-section instructor/enrollment requests (fewer requests, less data)
- `--concurrency N` — courses fetched in parallel per subject (default 10)
- `--skip-recent HOURS` — skip courses already scraped within this window
- `--section-delay SECONDS` — pause between per-section detail requests within a course, detailed mode only (default 0.1)

## 2. Run the API

```bash
uvicorn app.api:app --reload
```

Open `http://127.0.0.1:8000/` for the web UI — browse/filter scraped sections
and ask the agent questions from the browser, no curl needed. Interactive
API docs are at `http://127.0.0.1:8000/docs`. Key endpoints:

- `GET /` — web UI
- `GET /api` — JSON service info (what used to live at `/`)
- `GET /subjects` — list subject codes in the dataset
- `GET /courses/{subject}` — courses under a subject with section counts
- `GET /sections` — filterable section level query (subject, course_number, instructor, term)
- `GET /stats` — row counts and terms covered, good sanity check after a scrape
- `POST /ask` — plain English question -> SQL -> answer (needs `GROQ_API_KEY`)
- `POST /ask/stream` — same, streamed back as Server-Sent Events (what the web UI uses)

## 3. Ask it a question

```bash
export OPENAI_API_KEY=sk-...
python app/agent.py "Which CS courses have the most sections this fall?"
```

or through the API:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Who teaches CS 411 this semester?"}'
```

## Schema

Single table, `sections`, one row per course section:

| column | type | notes |
|---|---|---|
| year | int | e.g. 2026 |
| semester | text | lowercase: fall / spring / summer / winter |
| subject | text | uppercase subject code, e.g. CS |
| course_number | text | e.g. 225 |
| course_label | text | course title, e.g. "Data Structures" |
| crn | text | course reference number, unique per section per term |
| section_name | text | section letter/label from the course level listing |
| instructor | text | nullable, only populated in detailed mode |
| enrollment_status | text | nullable, only populated in detailed mode |
| credit_hours | text | as published, e.g. "3 hours" or "3 OR 4 hours" |
| description | text | catalog description, nullable, only populated in detailed mode, same text for every section of a course |
| scraped_at | timestamp | when the row was last written |

The agent (`agent.py` / `/ask`) is instructed to summarize `description` in
its own words when asked what a course is about, rather than pasting the
raw catalog text - which tends to be long and full of registrar boilerplate
(prerequisite chains, cross-listing notes, credit restrictions).

## Known limitations

- Cross listed courses (e.g. CS 440 / ECE 448) are stored as separate rows
  under each subject, since Course Explorer lists them separately. Not yet
  deduplicated.
- `enrollment_status` reflects whatever the API returned at scrape time,
  it is not live. Re-scrape to refresh.
- No diff mode within a run yet — courses are always fully re-fetched unless
  `--skip-recent` is set, in which case a course scraped within the window
  is skipped entirely. There's no partial diffing (e.g. only refresh
  enrollment status on an otherwise-fresh course).
- The LangChain agent has direct SQL execution access to the local SQLite
  file. Fine for a local read-only dataset like this; would need a scoped,
  read-only DB user before pointing it at anything with write access.
- **Course descriptions**: the scraper checks a few candidate XML tag names
  (`description`, `courseDescription`, `descr`) for the catalog module, since
  the exact schema couldn't be verified against the live API from the
  environment this was built in (same access issue documented above - the
  API was blocking non-browser requests at the time). If descriptions come
  back `NULL` for every course after a detailed scrape even though the scrape
  otherwise succeeds, the real tag name is probably something else. Fetch one
  URL directly to check, e.g.
  `https://courses.illinois.edu/cisapp/explorer/catalog/2026/fall/CS/225.xml`,
  see what the description element is actually called, and it's a one-line
  fix in `DESCRIPTION_TAGS` near the top of `fetch_course_description()` in
  `scraper.py`.

## Repo layout

```
course-explorer-agent/
├── app/
│   ├── scraper.py     # CISAPI scraper -> SQLite
│   ├── api.py          # FastAPI backend
│   └── agent.py        # LangChain NL -> SQL agent
├── static/
│   └── index.html      # web UI, served at / by api.py
├── data/
│   └── courses.db      # created after first scrape
├── docs/
│   └── architecture.html  # storage model, request flow, and RAG loop, illustrated
├── requirements.txt
└── README.md
```

For how the pieces fit together — the two storage layers, the schema, the
request path, and the multi-query RAG loop — open `docs/architecture.html`
in a browser. `DECISIONS.md` is the running log of *why* each choice was
made; `DEPLOYMENT.md` is the Render + Neon runbook.
