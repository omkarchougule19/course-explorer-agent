"""
ask_log.py

Persists every /ask attempt and enforces a lightweight per-client guardrail,
so a handful of users can't drain the Groq free-tier budget (200,000
tokens/day, ~80-100 real questions - see DECISIONS.md) by hammering the
assistant with junk.

Table `ask_log`:
    id, ts (UTC ISO), client_ip, question, outcome, answer_preview, latency_ms

`outcome`:
    answered       - the agent produced a real answer
    refused        - the agent declined it as out of scope (heuristic match on
                     the answer text)
    rate_limited   - blocked before the LLM by the per-IP limit
    global_limited - blocked before the LLM by the shared daily cap
    too_long       - blocked before the LLM by the length cap
    error          - the agent or LLM provider errored (includes quota)
    pending        - reserved, LLM call in flight (see reserve()/finish());
                     rewritten to one of the above when the call ends

`answered`, `refused` and `pending` count toward the rate limits - they mean
an LLM call was (or is being) spent. Each /ask reserves its `pending` row
before the LLM runs and the limit check counts rows in reservation order,
so a burst of parallel requests can't all pass on the same stale count.
`error` doesn't count, so a provider outage never locks users out;
`too_long` / `rate_limited` don't, since no call was made.

Read-side aggregates for the admin dashboard (all computed on demand, no
counter table): `unique_clients()` (distinct IPs, optional trailing window),
`outcome_counts()`, `clients()` (per-IP rollup - this app's stand-in for a
"sessions" list), `daily_counts()` (per-day volume), and `summary()` which
bundles the lot for the stat tiles. All use plain GROUP BY / COUNT and a
lexical `ts` compare so the one query text runs on SQLite and Postgres alike.

Writes have no auth (it's just a log). Reads are via GET /admin/ask-log,
gated on the ADMIN_TOKEN env var (endpoint 404s if that's unset).

Tunable via env vars, all with sane defaults:
    ASK_MAX_CHARS       (500)  reject questions longer than this
    ASK_RATE_PER_HOUR   (10)   max LLM-spending questions per IP per hour
    ASK_RATE_PER_DAY    (60)   ... per IP per day
    ASK_GLOBAL_PER_DAY  (250)  max LLM-spending questions across ALL clients
                               per day - the real backstop for the Groq
                               budget, since the per-IP limit keys on a
                               spoofable X-Forwarded-For
"""

import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from app import db

MAX_CHARS = int(os.environ.get("ASK_MAX_CHARS", "500"))
RATE_PER_HOUR = int(os.environ.get("ASK_RATE_PER_HOUR", "10"))
RATE_PER_DAY = int(os.environ.get("ASK_RATE_PER_DAY", "60"))
GLOBAL_PER_DAY = int(os.environ.get("ASK_GLOBAL_PER_DAY", "250"))

# Substrings that mark the agent's own scope-refusal, used only to tag the
# `refused` outcome for the dashboard. Loose on purpose - it's a signal, not
# a gate. Keep roughly in sync with the refusal phrasing SYSTEM_CONTEXT
# nudges the model toward (see app/agent.py).
_REFUSAL_MARKERS = (
    "only answer questions",
    "only able to answer",
    "can only help with",
    "out of scope",
    "can't comply",
    "cannot comply",
    "not a general-purpose assistant",
    "don't have information about",
    "can only answer questions that can be answered",
)

# Substrings agent.ask() uses when it returns a provider/setup problem as a
# plain string rather than raising - tagged `error`, and not counted against
# the rate limit.
_ERROR_MARKERS = (
    "rate limit was hit",
    "provider rejected the api key",
    "timed out",
    "something went wrong answering",
    "can't answer that right now",
    "needed more steps than i can take",
)


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ask_log (
            id {db.autoincrement_pk()},
            ts TEXT NOT NULL,
            client_ip TEXT,
            question TEXT NOT NULL,
            outcome TEXT NOT NULL,
            answer_preview TEXT,
            latency_ms INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ask_log_ts ON ask_log(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ask_log_ip ON ask_log(client_ip, ts)")
    conn.commit()


def classify_answer(answer: str) -> str:
    """Tag a completed agent answer as 'error', 'refused', or 'answered'."""
    low = (answer or "").lower()
    if any(m in low for m in _ERROR_MARKERS):
        return "error"
    if any(m in low for m in _REFUSAL_MARKERS):
        return "refused"
    return "answered"


# Outcomes that spend (or, for `pending`, are about to spend) an LLM call and
# so count toward every limit. `pending` is the slot reserve() claims before
# the LLM runs; finish() rewrites it to the real outcome afterwards.
_SPENDING = "('answered', 'refused', 'pending')"


def _spent(conn: db.Connection, since: timedelta, client_ip: "str | None" = None,
           upto_id: "int | None" = None) -> int:
    """LLM-spending rows in the trailing `since`, optionally for one IP, and
    optionally only rows up to and including `upto_id` - i.e. the calls that
    reserved a slot no later than that row did. Cutoffs are computed in Python
    so the query stays portable across SQLite and Postgres."""
    cutoff = (datetime.now(timezone.utc) - since).isoformat(timespec="seconds")
    sql = f"SELECT COUNT(*) AS n FROM ask_log WHERE ts >= ? AND outcome IN {_SPENDING}"
    params: list = [cutoff]
    if client_ip is not None:
        sql += " AND client_ip = ?"
        params.append(client_ip)
    if upto_id is not None:
        sql += " AND id <= ?"
        params.append(upto_id)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def over_limit(conn: db.Connection, client_ip: str,
               reserved_id: "int | None" = None) -> tuple[bool, str]:
    """(blocked, scope). Counts this IP's LLM-spending attempts (answered +
    refused + pending) in the trailing hour and day.

    With `reserved_id` (the caller's own pending row, see reserve()), only
    rows reserved up to and including it count, and the limit is exceeded
    once that count passes the cap - so of N simultaneous requests exactly
    the first cap-many get through, instead of all N reading the same
    pre-insert count."""
    if not client_ip or client_ip == "unknown":
        return False, ""
    for scope, delta, cap in (("hour", timedelta(hours=1), RATE_PER_HOUR),
                              ("day", timedelta(days=1), RATE_PER_DAY)):
        n = _spent(conn, delta, client_ip, reserved_id)
        if (n > cap) if reserved_id is not None else (n >= cap):
            return True, scope
    return False, ""


def global_over_limit(conn: db.Connection, reserved_id: "int | None" = None) -> bool:
    """True once the whole app has spent ASK_GLOBAL_PER_DAY LLM calls in the
    trailing 24h (answered + refused + pending). Unlike over_limit() this keys
    on nothing client-controlled, so it holds even against X-Forwarded-For
    spoofing - it's the actual protection for the provider's daily budget.
    `reserved_id` works as in over_limit()."""
    n = _spent(conn, timedelta(days=1), upto_id=reserved_id)
    return n > GLOBAL_PER_DAY if reserved_id is not None else n >= GLOBAL_PER_DAY


def record(conn: db.Connection, client_ip: str, question: str, outcome: str,
           answer: "str | None" = None, latency_ms: "int | None" = None) -> int:
    """Insert one row (committed immediately, so concurrent requests see it)
    and return its id."""
    params = (
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        client_ip,
        (question or "")[:1000],
        outcome,
        (answer or "")[:500] or None,
        latency_ms,
    )
    sql = ("INSERT INTO ask_log (ts, client_ip, question, outcome, answer_preview, latency_ms) "
           "VALUES (?, ?, ?, ?, ?, ?)")
    if conn.backend == "postgres":
        row_id = conn.execute(sql + " RETURNING id", params).fetchone()["id"]
    else:
        row_id = conn.execute(sql, params).lastrowid
    conn.commit()
    return int(row_id)


def reserve(conn: db.Connection, client_ip: str, question: str) -> int:
    """Claim a `pending` slot before calling the LLM. The row counts toward
    every limit from the moment it's committed, so the check-then-spend race
    (N parallel requests all reading the same count) is closed. Always pair
    with finish()."""
    return record(conn, client_ip, question, "pending")


def finish(conn: db.Connection, row_id: int, outcome: str,
           answer: "str | None" = None, latency_ms: "int | None" = None) -> None:
    """Rewrite a reserve()d row with the real outcome."""
    conn.execute(
        "UPDATE ask_log SET outcome = ?, answer_preview = ?, latency_ms = ? WHERE id = ?",
        (outcome, (answer or "")[:500] or None, latency_ms, row_id),
    )
    conn.commit()


def recent(conn: db.Connection, limit: int = 100, ip: "str | None" = None,
           outcome: "str | None" = None) -> list[dict]:
    """Most recent log entries, newest first - for GET /admin/ask-log."""
    query = "SELECT id, ts, client_ip, question, outcome, answer_preview, latency_ms FROM ask_log"
    params: list = []
    clauses = []
    if ip:
        clauses.append("client_ip = ?")
        params.append(ip)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    return [dict(r) for r in conn.execute(query, params).fetchall()]


# --------------------------------------------------------------------------
# Read-side aggregates for GET /admin/ask-stats, /admin/clients, /admin/activity
# and the public GET /ask/summary. Nothing here writes - every number is
# computed from ask_log on demand (the data lives in Neon, not process memory,
# so it survives Render restarts). `ts` is compared as text since ISO-8601 UTC
# sorts lexically, keeping one query text valid on both SQLite and Postgres.
# --------------------------------------------------------------------------

def _cutoff(since: "timedelta | None") -> "str | None":
    if since is None:
        return None
    return (datetime.now(timezone.utc) - since).isoformat(timespec="seconds")


def _window(since: "timedelta | None", glue: str = "WHERE") -> tuple:
    """('', []) for no window, else (' <glue> ts >= ?', [cutoff]) - `glue` is
    WHERE for a fresh clause or AND to extend an existing one."""
    cutoff = _cutoff(since)
    return (f" {glue} ts >= ?", [cutoff]) if cutoff else ("", [])


def _scalar(conn: db.Connection, sql: str, params: list) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def _count_since(conn: db.Connection, since: "timedelta | None" = None) -> int:
    """Total ask_log rows (any outcome) in the trailing window, or ever."""
    w, p = _window(since)
    return _scalar(conn, "SELECT COUNT(*) AS n FROM ask_log" + w, p)


def unique_clients(conn: db.Connection, since: "timedelta | None" = None) -> int:
    """Distinct client IPs in ask_log, optionally within the trailing window.
    Excludes the '' / 'unknown' placeholder _client_ip() falls back to when
    there's no usable X-Forwarded-For (NULL is excluded by the NOT IN too), so
    it counts identifiable clients only - a spoofable, NAT-collapsed floor,
    not analytics."""
    w, p = _window(since, "AND")
    return _scalar(
        conn,
        "SELECT COUNT(DISTINCT client_ip) AS n FROM ask_log "
        "WHERE client_ip NOT IN ('', 'unknown')" + w,
        p,
    )


def outcome_counts(conn: db.Connection, since: "timedelta | None" = None) -> dict:
    """{outcome: count} over ask_log, optionally windowed. Outcomes with no
    rows are simply absent - callers use .get(name, 0)."""
    w, p = _window(since)
    rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM ask_log" + w + " GROUP BY outcome", p
    ).fetchall()
    return {r["outcome"]: int(r["n"]) for r in rows}


def clients(conn: db.Connection, limit: int = 100) -> list:
    """Per-client-IP rollup, most-recently-active first: how many questions
    that IP asked, when it was first and last seen, and how many of those
    spent an actual model call (answered + refused, matching what the rate
    limit counts). The closest thing to a session list without accounts."""
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        "SELECT client_ip, "
        "COUNT(*) AS questions, "
        "MIN(ts) AS first_seen, "
        "MAX(ts) AS last_seen, "
        "SUM(CASE WHEN outcome IN ('answered', 'refused') THEN 1 ELSE 0 END) AS llm_calls "
        "FROM ask_log "
        "WHERE client_ip NOT IN ('', 'unknown') "
        "GROUP BY client_ip "
        "ORDER BY MAX(ts) DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "client_ip": r["client_ip"],
            "questions": int(r["questions"]),
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "llm_calls": int(r["llm_calls"] or 0),
        }
        for r in rows
    ]


def daily_counts(conn: db.Connection, days: int = 30) -> list:
    """Questions and distinct clients per calendar day (UTC) over the trailing
    `days`. `substr(ts, 1, 10)` takes the YYYY-MM-DD prefix and behaves the
    same on SQLite and Postgres because `ts` is an ISO-8601 text string."""
    days = max(1, min(int(days), 90))
    cutoff = _cutoff(timedelta(days=days))
    rows = conn.execute(
        "SELECT substr(ts, 1, 10) AS day, "
        "COUNT(*) AS questions, "
        "COUNT(DISTINCT client_ip) AS uniq "
        "FROM ask_log WHERE ts >= ? "
        "GROUP BY substr(ts, 1, 10) "
        "ORDER BY day",
        (cutoff,),
    ).fetchall()
    return [
        {"day": r["day"], "questions": int(r["questions"]), "uniq": int(r["uniq"])}
        for r in rows
    ]


def trending_courses(conn: db.Connection, subjects: "set[str]", since: timedelta = timedelta(days=7),
                      limit: int = 6) -> list[str]:
    """Which course codes show up most in recent questions - for the
    homepage's suggested-question chips. Deliberately returns catalog course
    codes only (e.g. "CS 225"), never the question text itself: /ask/summary
    and this function both hold the line that raw free-text questions never
    become public, since a student could have typed anything into that box.
    A course code isn't personal - it's the same public catalog data Browse
    Sections already shows for anyone."""
    if not subjects:
        return []
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(s) for s in subjects) + r")\s?(\d{2,3}[A-Z]?)\b",
        re.IGNORECASE,
    )
    w, p = _window(since, "AND")
    rows = conn.execute(
        "SELECT question FROM ask_log WHERE outcome IN ('answered', 'refused')" + w,
        p,
    ).fetchall()

    counts: Counter = Counter()
    for r in rows:
        for subj, num in pattern.findall(r["question"] or ""):
            counts[f"{subj.upper()} {num.upper()}"] += 1
    return [code for code, _ in counts.most_common(limit)]


def summary(conn: db.Connection) -> dict:
    """Everything the dashboard's stat tiles need, in one call. Any DB error
    yields a zeroed dict rather than propagating - a slow Neon wake must not
    500 the admin page."""
    day, week = timedelta(days=1), timedelta(days=7)
    try:
        return {
            "unique_24h": unique_clients(conn, day),
            "unique_7d": unique_clients(conn, week),
            "unique_all": unique_clients(conn),
            "questions_24h": _count_since(conn, day),
            "questions_7d": _count_since(conn, week),
            "outcomes_24h": outcome_counts(conn, day),
            "outcomes_all": outcome_counts(conn),
        }
    except Exception as exc:  # noqa: BLE001 - dashboard must render regardless
        print(f"[ask_log.summary] failed: {exc!r}", flush=True)
        return {
            "unique_24h": 0, "unique_7d": 0, "unique_all": 0,
            "questions_24h": 0, "questions_7d": 0,
            "outcomes_24h": {}, "outcomes_all": {},
        }
