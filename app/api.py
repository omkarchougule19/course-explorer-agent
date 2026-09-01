"""
api.py

FastAPI backend exposing the scraped Course Explorer dataset.

Run:
    uvicorn app.api:app --reload

Docs:
    http://127.0.0.1:8000/docs
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app import sync_requests as sync_reqs
from app import ask_log as ask_log_mod
from app import feedback as feedback_mod
from app.db import DB_PATH

load_dotenv(Path(__file__).parent.parent / ".env")

STATIC_DIR = Path(__file__).parent.parent / "static"

_DOCS_ON = bool(os.environ.get("ENABLE_DOCS"))

app = FastAPI(
    title="UIUC Course Explorer Data Agent",
    description="Catalog of UIUC course, section, and enrollment data scraped from the public Course Explorer API.",
    version="1.0.0",
    # Interactive docs and the raw OpenAPI schema are off unless ENABLE_DOCS
    # is set - they're an information-disclosure surface not needed in prod.
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

# Baseline security headers on every response. CSP allows the page's own
# inline <script>/<style> and the Google Fonts it loads; nothing else.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
}


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort safety net. Log the real error server-side; return a generic
    # message so driver/SQL/stack details never reach the client.
    import traceback
    print(f"[unhandled] {request.method} {request.url.path}: {exc!r}", flush=True)
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.on_event("startup")
def _warmup_embeddings():
    """Load the embedding model now, during boot, instead of lazily on the
    first RAG question - the local ONNX load (~1-2s once the model is baked
    into the build, see render.yaml) happens while Render's own container
    spin-up is already in progress, not stacked onto a user's first request.
    See DECISIONS.md for the full cold-start reasoning. Postgres-only: the
    local SQLite dev fallback never registers the vector-search tool at all,
    so there's nothing to warm up there."""
    if db.is_postgres():
        from app import embeddings
        embeddings.warmup()


@app.on_event("startup")
def _ensure_app_tables():
    """Create the app's own bookkeeping tables (sync_requests, ask_log) so
    their routes work against a fresh database, before any CLI has run."""
    try:
        conn = db.get_connection()
    except Exception:
        return  # DB unreachable at boot - routes will surface it per-request
    try:
        sync_reqs.init_table(conn)
        ask_log_mod.init_table(conn)
        feedback_mod.init_table(conn)
    finally:
        conn.close()


@contextmanager
def get_conn():
    """Open a connection for the duration of a request, always closing it,
    and turning DB-open failures into a clean 503 instead of a raw traceback.
    The "does the database exist" pre-check only applies to the local SQLite
    fallback - a Postgres DATABASE_URL is presumed to point at something that
    already exists, and a real connection failure surfaces below instead."""
    if not db.is_postgres() and not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Database not found. Run scraper.py first.")
    try:
        conn = db.get_connection()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Couldn't open database: {exc}")
    try:
        yield conn
    finally:
        conn.close()


def run_query(conn: db.Connection, query: str, params: list):
    """Run a SELECT and turn any database error into a clean 500 instead of
    crashing the route. The driver error text is logged, not returned - it can
    echo SQL fragments and backend internals."""
    try:
        return conn.execute(query, params).fetchall()
    except Exception as exc:
        print(f"[query-failed] {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="Query failed")


class SectionOut(BaseModel):
    year: int
    semester: str
    subject: str
    course_number: str
    course_label: Optional[str]
    crn: str
    section_name: Optional[str]
    instructor: Optional[str]
    enrollment_status: Optional[str]
    credit_hours: Optional[str]
    description: Optional[str] = None


@app.get("/api")
def api_info():
    return {
        "service": "UIUC Course Explorer Data Agent",
        "ui": "/",
        "endpoints": ["/subjects", "/courses/{subject}", "/sections", "/stats", "/ask"],
    }


@app.get("/subjects")
def get_subjects(year: Optional[int] = None, semester: Optional[str] = None):
    """List every subject code present in the dataset, optionally filtered by term."""
    query = "SELECT DISTINCT subject FROM sections"
    params: list = []
    clauses = []
    if year:
        clauses.append("year = ?")
        params.append(year)
    if semester:
        clauses.append("semester = ?")
        params.append(semester.lower())
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY subject"

    with get_conn() as conn:
        rows = run_query(conn, query, params)
    return {"subjects": [r["subject"] for r in rows]}


@app.get("/courses/{subject}")
def get_courses(subject: str, year: Optional[int] = None, semester: Optional[str] = None):
    """List distinct courses under a subject, with section counts."""
    if not subject or not subject.strip():
        raise HTTPException(status_code=400, detail="subject is required")

    query = """
        SELECT course_number, course_label, COUNT(*) as section_count
        FROM sections WHERE subject = ?
    """
    params: list = [subject.strip().upper()]
    if year:
        query += " AND year = ?"
        params.append(year)
    if semester:
        query += " AND semester = ?"
        params.append(semester.lower())
    query += " GROUP BY course_number, course_label ORDER BY course_number"

    with get_conn() as conn:
        rows = run_query(conn, query, params)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No courses found for subject {subject.strip().upper()}")
    return {"subject": subject.strip().upper(), "courses": [dict(r) for r in rows]}


def _course_level(course_number: Optional[str]) -> Optional[str]:
    """UIUC course-number convention (catalog.illinois.edu): <400 undergrad,
    400-499 undergrad + graduate, >=500 graduate. Returns 'undergrad',
    '400level', 'grad', or None if no leading number can be read."""
    if not course_number:
        return None
    digits = ""
    for ch in str(course_number):
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    n = int(digits)
    if n >= 500:
        return "grad"
    if n >= 400:
        return "400level"
    return "undergrad"


@app.get("/sections", response_model=list[SectionOut])
def get_sections(
    subject: Optional[str] = None,
    course_number: Optional[str] = None,
    year: Optional[int] = None,
    semester: Optional[str] = None,
    instructor: Optional[str] = None,
    level: Optional[str] = Query(default=None, description="undergrad | 400level | grad"),
    limit: int = Query(default=100, le=1000, ge=1),
):
    """Query individual sections with optional filters."""
    query = "SELECT * FROM sections WHERE 1=1"
    params: list = []
    if subject:
        query += " AND subject = ?"
        params.append(subject.upper())
    if course_number:
        query += " AND course_number = ?"
        params.append(course_number)
    if year:
        query += " AND year = ?"
        params.append(year)
    if semester:
        query += " AND semester = ?"
        params.append(semester.lower())
    if instructor:
        query += " AND instructor LIKE ?"
        params.append(f"%{instructor}%")

    # Level is derived from the course number, which is stored as TEXT and
    # can carry a trailing letter ("492A") - not something to CAST portably
    # in SQL. Filter it in Python: pull a wider set (capped), then trim to
    # `limit`. Without a level filter, keep the plain SQL LIMIT.
    level = level.lower() if level else None
    if level in ("undergrad", "400level", "grad"):
        query += " LIMIT ?"
        params.append(min(5000, max(limit * 20, 1000)))
        with get_conn() as conn:
            rows = run_query(conn, query, params)
        filtered = [dict(r) for r in rows if _course_level(r["course_number"]) == level]
        return filtered[:limit]

    query += " LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = run_query(conn, query, params)
    return [dict(r) for r in rows]


@app.get("/sync/status")
def get_sync_status():
    """Per-department demand + freshness for the UI's Departments panel and
    the operator CLI: pending sync-request count, last-synced age, section
    count. Public, no auth - it's not sensitive."""
    with get_conn() as conn:
        return {"departments": sync_reqs.status(conn)}


class SyncRequest(BaseModel):
    subject: str


@app.post("/sync/request")
def post_sync_request(payload: SyncRequest):
    """Register demand to refresh one department. Bumps a counter the operator
    ranks by when deciding what to re-scrape locally. No rate limit by design
    (see DECISIONS.md) - repeated clicks just raise the number."""
    subject = (payload.subject or "").strip().upper()
    # ASCII letters only, 2-12 chars: real UIUC subject codes (CS, ECE, MATH,
    # ...). isascii() + isalpha() together reject Unicode "letters" that could
    # smuggle markup or just junk into the operator's demand panel.
    if not (2 <= len(subject) <= 12 and subject.isascii() and subject.isalpha()):
        raise HTTPException(status_code=400, detail="subject must be a 2-12 letter code, e.g. MECH")
    with get_conn() as conn:
        count = sync_reqs.record_request(conn, subject)
    return {"subject": subject, "pending_count": count}


class AskRequest(BaseModel):
    question: str
    # Optional windowed conversation history from the browser: a list of
    # {"q": ..., "a": ...} prior turns. The server re-trims it (last 3 turns,
    # each clipped) in agent.build_agent_input - it's context only, never
    # trusted for length or re-answered.
    history: Optional[list] = Field(default=None, max_length=20)


class FeedbackRequest(BaseModel):
    vote: Literal["up", "down"]
    question: str
    answer: str
    # Windowed {q,a} history snapshot from the browser. Stored (downvotes
    # only) so a reviewer sees the whole exchange; re-trimmed server-side in
    # app/feedback.py. Never trusted for anything but the review queue.
    history: Optional[list] = Field(default=None, max_length=20)


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the per-IP guardrail. Behind Render's proxy
    the client is the first X-Forwarded-For hop. This is trivially spoofable,
    so the per-IP limit is only friction - the ASK_GLOBAL_PER_DAY cap, which
    keys on nothing client-controlled, is the real budget protection."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _ask_precheck(question: str, ip: str) -> Optional[tuple[int, str]]:
    """Run the pre-LLM guardrails shared by /ask and /ask/stream: length cap,
    shared daily cap, per-IP rate limit. Records the blocking outcome to
    ask_log and returns (status_code, detail) if blocked, else None. See
    app/ask_log.py and DECISIONS.md for the rationale."""
    with get_conn() as conn:
        if len(question) > ask_log_mod.MAX_CHARS:
            ask_log_mod.record(conn, ip, question, "too_long")
            return 422, (f"That question is {len(question)} characters; the limit is "
                         f"{ask_log_mod.MAX_CHARS}. Ask something shorter and more specific.")
        if ask_log_mod.global_over_limit(conn):
            ask_log_mod.record(conn, ip, question, "global_limited")
            return 429, ("The assistant has reached its shared daily limit. Browse "
                         "Sections and Department Data still work; try the assistant "
                         "again tomorrow.")
        blocked, scope = ask_log_mod.over_limit(conn, ip)
        if blocked:
            ask_log_mod.record(conn, ip, question, "rate_limited")
            return 429, (f"You've hit the limit of AI questions per {scope}. The Browse "
                         f"Sections and Department Data tools still work, and you can ask "
                         f"the assistant again later.")
    return None


def _sse(event: str, data: str) -> str:
    """Format one Server-Sent Events frame. The data is JSON-encoded so
    embedded newlines don't break SSE's line-oriented framing."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/ask")
def ask_agent(payload: AskRequest, request: Request):
    """Plain-English question -> SQL/vector agent -> natural-language answer.
    Requires GROQ_API_KEY (or GEMINI/OPENAI) on the server. Every attempt is
    written to ask_log; a per-IP rate limit and a length cap run before the
    LLM so junk can't drain the provider's daily budget (see app/ask_log.py
    and DECISIONS.md). The browser UI uses /ask/stream instead; this stays as
    the non-streaming fallback and the documented curl entry point."""
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question can't be empty")

    ip = _client_ip(request)

    blocked = _ask_precheck(question, ip)
    if blocked:
        raise HTTPException(status_code=blocked[0], detail=blocked[1])

    try:
        from app.agent import ask
    except ImportError as exc:
        print(f"[ask] agent import failed: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="Assistant is unavailable")

    # agent.ask() catches setup/provider problems and returns them as a plain
    # string; this only guards against something truly unexpected.
    t0 = time.monotonic()
    try:
        answer = ask(question, history=payload.history)
    except Exception as exc:  # noqa: BLE001 - defense in depth
        latency = int((time.monotonic() - t0) * 1000)
        print(f"[ask] agent raised: {exc!r}", flush=True)
        with get_conn() as conn:
            ask_log_mod.record(conn, ip, question, "error", str(exc), latency)
        raise HTTPException(status_code=502, detail="The assistant failed to answer. Try again shortly.")

    latency = int((time.monotonic() - t0) * 1000)
    outcome = ask_log_mod.classify_answer(answer)  # answered | refused | error
    with get_conn() as conn:
        ask_log_mod.record(conn, ip, question, outcome, answer, latency)
    return {"question": question, "answer": answer}


@app.post("/ask/stream")
async def ask_agent_stream(payload: AskRequest, request: Request):
    """Same contract as /ask, but streams the answer as Server-Sent Events so
    the UI can render it token-by-token with a live "Running SQL…" status.

    Frames: `status` (progress label), `token` (answer delta), `done` (the
    full authoritative answer, emitted once), `error` (unexpected failure).
    Guardrails run synchronously before the stream opens, so a blocked call
    still returns a normal JSON error, not a stream."""
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question can't be empty")

    ip = _client_ip(request)

    blocked = _ask_precheck(question, ip)
    if blocked:
        raise HTTPException(status_code=blocked[0], detail=blocked[1])

    try:
        from app.agent import astream_answer
    except ImportError as exc:
        print(f"[ask/stream] agent import failed: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="Assistant is unavailable")

    async def event_stream():
        t0 = time.monotonic()
        parts: list[str] = []
        final_text = ""
        try:
            async for kind, text in astream_answer(question, payload.history):
                if kind == "token":
                    parts.append(text)
                    yield _sse("token", text)
                elif kind == "status":
                    yield _sse("status", text)
                elif kind == "done":
                    final_text = text
                    yield _sse("done", text)
        except Exception as exc:  # noqa: BLE001 - defense in depth
            print(f"[ask/stream] agent raised: {exc!r}", flush=True)
            yield _sse("error", "The assistant failed to answer. Try again shortly.")
            if not final_text:
                final_text = "Something went wrong answering that question."
        finally:
            latency = int((time.monotonic() - t0) * 1000)
            answer = final_text or "".join(parts)
            outcome = ask_log_mod.classify_answer(answer)
            try:
                with get_conn() as conn:
                    ask_log_mod.record(conn, ip, question, outcome, answer, latency)
            except Exception as exc:  # noqa: BLE001 - logging must not break the response
                print(f"[ask/stream] ask_log write failed: {exc!r}", flush=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell any proxy not to buffer the stream
        },
    )


@app.post("/ask/feedback")
def post_ask_feedback(payload: FeedbackRequest, request: Request):
    """Record a thumbs up/down on an assistant answer. Public and
    best-effort: any storage failure returns {"ok": false} with HTTP 200
    rather than an error, so a thumbs click never breaks the page. Downvotes
    also snapshot the chat history for the biweekly quality review (see
    app/feedback.py)."""
    ip = _client_ip(request)
    try:
        with get_conn() as conn:
            ok = feedback_mod.record(
                conn, ip, payload.vote, payload.question, payload.answer,
                payload.history,
            )
    except Exception as exc:  # noqa: BLE001 - a feedback click must never 5xx
        print(f"[ask/feedback] write failed: {exc!r}", flush=True)
        ok = False
    return {"ok": bool(ok)}


def _require_admin(request: Request, token: Optional[str]) -> None:
    """Gate for every /admin/* route. Requires the ADMIN_TOKEN env var set on
    the server AND supplied via ?token= or the X-Admin-Token header. Always
    raises the same 403 on any failure (unset, missing, or wrong) so the
    response never reveals whether the token is even configured."""
    import hmac

    expected = os.environ.get("ADMIN_TOKEN")
    supplied = token or request.headers.get("x-admin-token")
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/admin/ask-log")
def admin_ask_log(
    request: Request,
    token: Optional[str] = None,
    ip: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = Query(default=100, le=1000, ge=1),
):
    """Recent /ask activity - question text, outcome, client IP, latency.
    ADMIN_TOKEN-gated (see _require_admin)."""
    _require_admin(request, token)
    with get_conn() as conn:
        return {"entries": ask_log_mod.recent(conn, limit=limit, ip=ip, outcome=outcome)}


@app.get("/admin/ask-stats")
def admin_ask_stats(request: Request, token: Optional[str] = None):
    """Aggregate assistant-usage numbers for the dashboard stat tiles: unique
    clients (24h / 7d / all-time), question volume, outcome breakdown, and the
    feedback up/down tallies. All computed on demand from ask_log /
    answer_feedback - no counter table."""
    _require_admin(request, token)
    with get_conn() as conn:
        s = ask_log_mod.summary(conn)
        try:
            s["feedback"] = feedback_mod.counts(conn)
        except Exception as exc:  # noqa: BLE001 - the feedback tile is optional
            print(f"[admin/ask-stats] feedback counts failed: {exc!r}", flush=True)
            s["feedback"] = {"up": 0, "down": 0, "down_unreviewed": 0}
    return {"summary": s}


@app.get("/admin/clients")
def admin_clients(
    request: Request,
    token: Optional[str] = None,
    limit: int = Query(default=100, le=1000, ge=1),
):
    """Per-client-IP rollup - the app's stand-in for a 'sessions' list, since
    there are no accounts. Most recently active first."""
    _require_admin(request, token)
    with get_conn() as conn:
        return {"clients": ask_log_mod.clients(conn, limit)}


@app.get("/admin/activity")
def admin_activity(
    request: Request,
    token: Optional[str] = None,
    days: int = Query(default=30, le=90, ge=1),
):
    """Questions + distinct clients per UTC day over the trailing `days`, for
    the dashboard's activity chart."""
    _require_admin(request, token)
    with get_conn() as conn:
        return {"daily": ask_log_mod.daily_counts(conn, days)}


@app.get("/admin/feedback")
def admin_feedback(
    request: Request,
    token: Optional[str] = None,
    vote: Optional[str] = None,
    reviewed: Optional[int] = None,
    limit: int = Query(default=100, le=1000, ge=1),
):
    """Thumbs up/down rows for the review panel. `vote=down&reviewed=0` is the
    biweekly triage queue. `reviewed`: 0 = not yet reviewed, 1 = reviewed,
    omitted = all."""
    _require_admin(request, token)
    reviewed_flag = None if reviewed is None else bool(reviewed)
    with get_conn() as conn:
        return {
            "feedback": feedback_mod.recent(
                conn, vote=vote, reviewed=reviewed_flag, limit=limit
            )
        }


@app.post("/admin/feedback/{feedback_id}/reviewed")
def admin_feedback_reviewed(
    feedback_id: int, request: Request, token: Optional[str] = None
):
    """Mark one downvote row as handled (stamps reviewed_at). `already` is
    true if it was already reviewed or the id doesn't exist."""
    _require_admin(request, token)
    with get_conn() as conn:
        changed = feedback_mod.mark_reviewed(conn, feedback_id)
    return {"ok": True, "id": feedback_id, "already": not changed}


@app.get("/freshness")
def get_freshness():
    """Last-updated timestamp and row count per (subject, year, semester) already
    in the database - i.e. how stale each subject/term's data is, since nothing
    here is live (see DECISIONS.md: scraping only ever runs locally, manually)."""
    with get_conn() as conn:
        rows = run_query(
            conn,
            """
            SELECT subject, year, semester, MAX(scraped_at) as last_updated, COUNT(*) as section_count
            FROM sections
            GROUP BY subject, year, semester
            ORDER BY subject, year, semester
            """,
            [],
        )
    return {"freshness": [dict(r) for r in rows]}


@app.get("/stats")
def get_stats():
    """High level counts, useful as a sanity check after scraping."""
    with get_conn() as conn:
        total = run_query(conn, "SELECT COUNT(*) as n FROM sections", [])[0]["n"]
        subjects = run_query(conn, "SELECT COUNT(DISTINCT subject) as n FROM sections", [])[0]["n"]
        courses = run_query(
            conn, "SELECT COUNT(DISTINCT subject || course_number) as n FROM sections", []
        )[0]["n"]
        terms = run_query(conn, "SELECT DISTINCT year, semester FROM sections ORDER BY year, semester", [])
    return {
        "total_sections": total,
        "distinct_subjects": subjects,
        "distinct_courses": courses,
        "terms_covered": [f"{r['semester']} {r['year']}" for r in terms],
    }


@app.get("/ask/summary")
def get_ask_summary():
    """Public, non-sensitive: how many distinct clients have used the
    assistant in the last 7 days - just an integer for the landing-page KPI
    tile. No IPs, no question text. Fails soft to 0 so a cold Neon wake can't
    break the page."""
    try:
        with get_conn() as conn:
            return {"unique_7d": ask_log_mod.unique_clients(conn, timedelta(days=7))}
    except Exception as exc:  # noqa: BLE001 - KPI tile must not 5xx the page
        print(f"[ask/summary] failed: {exc!r}", flush=True)
        return {"unique_7d": 0}


class ConflictCheckRequest(BaseModel):
    # Cap the list: the comparison is O(n^2) over meetings, and a real
    # schedule is a handful of sections. 50 is generous.
    crns: list[str] = Field(max_length=50)
    year: int = Field(ge=2000, le=2100)
    semester: str = Field(max_length=10)


_TIME_FMT = "%I:%M %p"


def _parse_time(raw: Optional[str]):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), _TIME_FMT).time()
    except ValueError:
        return None


def _days_overlap(days1: Optional[str], days2: Optional[str]) -> bool:
    return bool(set(days1 or "") & set(days2 or ""))


def _times_overlap(start1, end1, start2, end2) -> bool:
    if not (start1 and end1 and start2 and end2):
        return False
    return start1 < end2 and start2 < end1


@app.post("/schedule/conflicts")
def check_schedule_conflicts(payload: ConflictCheckRequest):
    """Given a set of CRNs for one term, report any pairs whose meetings overlap
    in both day and time. Sections with no meeting data (e.g. fully online/async)
    are silently skipped for that pair rather than flagged - there's nothing to
    compare."""
    crns = [c.strip() for c in payload.crns if c.strip()]
    if len(crns) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 CRNs to check for conflicts")

    placeholders = ",".join(["?"] * len(crns))
    with get_conn() as conn:
        rows = run_query(
            conn,
            f"""
            SELECT crn, meeting_type, days_of_week, start_time, end_time, building, room
            FROM meetings
            WHERE year = ? AND semester = ? AND crn IN ({placeholders})
            """,
            [payload.year, payload.semester.lower(), *crns],
        )

    meetings_by_crn: dict[str, list[dict]] = {}
    for r in rows:
        meetings_by_crn.setdefault(r["crn"], []).append(dict(r))

    conflicts = []
    for i, crn_a in enumerate(crns):
        for crn_b in crns[i + 1:]:
            for ma in meetings_by_crn.get(crn_a, []):
                for mb in meetings_by_crn.get(crn_b, []):
                    if not _days_overlap(ma["days_of_week"], mb["days_of_week"]):
                        continue
                    if _times_overlap(
                        _parse_time(ma["start_time"]), _parse_time(ma["end_time"]),
                        _parse_time(mb["start_time"]), _parse_time(mb["end_time"]),
                    ):
                        conflicts.append({"crn_a": crn_a, "meeting_a": ma, "crn_b": crn_b, "meeting_b": mb})

    return {"crns_checked": crns, "conflicts": conflicts, "has_conflicts": bool(conflicts)}


GRADE_WEIGHTS = {
    "a_plus": 4.0, "a": 4.0, "a_minus": 3.67,
    "b_plus": 3.33, "b": 3.0, "b_minus": 2.67,
    "c_plus": 2.33, "c": 2.0, "c_minus": 1.67,
    "d_plus": 1.33, "d": 1.0, "d_minus": 0.67,
    "f": 0.0,
}


@app.get("/courses/{subject}/{course_number}/grade-trend")
def get_grade_trend(subject: str, course_number: str, instructor: Optional[str] = None):
    """Per-term grade distribution and computed average GPA for a course, optionally
    filtered to one instructor. One row per (term, sched type, instructor) as recorded
    in grade_distributions - not collapsed across instructors, so trends per-instructor
    are visible rather than averaged away."""
    grade_cols = ", ".join(GRADE_WEIGHTS)
    query = f"""
        SELECT year, term, year_term, sched_type, primary_instructor,
               {grade_cols}, w, students
        FROM grade_distributions
        WHERE subject = ? AND course_number = ?
    """
    params: list = [subject.strip().upper(), course_number.strip()]
    if instructor:
        query += " AND primary_instructor LIKE ?"
        params.append(f"%{instructor}%")
    query += " ORDER BY year, term"

    with get_conn() as conn:
        rows = run_query(conn, query, params)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No grade distribution data for {subject.strip().upper()} {course_number.strip()}",
        )

    trend = []
    for r in rows:
        row = dict(r)
        graded = sum((row.get(col) or 0) for col in GRADE_WEIGHTS)
        points = sum((row.get(col) or 0) * weight for col, weight in GRADE_WEIGHTS.items())
        row["average_gpa"] = round(points / graded, 3) if graded else None
        trend.append(row)

    return {"subject": subject.strip().upper(), "course_number": course_number.strip(), "trend": trend}


# Serves static/index.html at "/" (the web UI) and any other files under static/.
# Registered last so it only catches paths not already claimed by the API routes
# above - FastAPI matches explicit routes first, in the order they were declared.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
