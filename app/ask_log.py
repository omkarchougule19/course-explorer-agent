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

Only `answered` and `refused` count toward the rate limit - both mean an LLM
call was actually spent. `error` doesn't, so a provider outage never locks
users out; `too_long` / `rate_limited` don't, since no call was made.

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


def over_limit(conn: db.Connection, client_ip: str) -> tuple[bool, str]:
    """(blocked, scope). Counts this IP's LLM-spending attempts (answered +
    refused) in the trailing hour and day. Cutoffs are computed in Python so
    the query stays portable across SQLite and Postgres."""
    if not client_ip or client_ip == "unknown":
        return False, ""
    now = datetime.now(timezone.utc)

    def count_since(delta: timedelta) -> int:
        cutoff = (now - delta).isoformat(timespec="seconds")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM ask_log "
            "WHERE client_ip = ? AND ts >= ? AND outcome IN ('answered', 'refused')",
            (client_ip, cutoff),
        ).fetchone()
        return int(row["n"]) if row else 0

    if count_since(timedelta(hours=1)) >= RATE_PER_HOUR:
        return True, "hour"
    if count_since(timedelta(days=1)) >= RATE_PER_DAY:
        return True, "day"
    return False, ""


def global_over_limit(conn: db.Connection) -> bool:
    """True once the whole app has spent ASK_GLOBAL_PER_DAY LLM calls in the
    trailing 24h (answered + refused). Unlike over_limit() this keys on
    nothing client-controlled, so it holds even against X-Forwarded-For
    spoofing - it's the actual protection for the provider's daily budget."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ask_log "
        "WHERE ts >= ? AND outcome IN ('answered', 'refused')",
        (cutoff,),
    ).fetchone()
    return (int(row["n"]) if row else 0) >= GLOBAL_PER_DAY


def record(conn: db.Connection, client_ip: str, question: str, outcome: str,
           answer: "str | None" = None, latency_ms: "int | None" = None) -> None:
    conn.execute(
        "INSERT INTO ask_log (ts, client_ip, question, outcome, answer_preview, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            client_ip,
            (question or "")[:1000],
            outcome,
            (answer or "")[:500] or None,
            latency_ms,
        ),
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
# and the public GET /ask/summary. Nothing here writes; everything is computed
# from ask_log on demand, so the numbers survive Render restarts for free (the
# data lives in Neon, not process memory). Cutoffs are built in Python and the
# `ts` column is compared as text - ISO-8601 UTC sorts correctly lexically - so
# the same SQL runs on SQLite and Postgres.
# --------------------------------------------------------------------------

def _cutoff(since: "timedelta | None") -> "str | None":
    if since is None:
        return None
    return (datetime.now(timezone.utc) - since).isoformat(timespec="seconds")


def _count_since(conn: db.Connection, since: "timedelta | None" = None) -> int:
    """Total ask_log rows (any outcome) in the trailing window, or ever."""
    sql = "SELECT COUNT(*) AS n FROM ask_log"
    params: list = []
    cutoff = _cutoff(since)
    if cutoff is not None:
        sql += " WHERE ts >= ?"
        params.append(cutoff)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def unique_clients(conn: db.Connection, since: "timedelta | None" = None) -> int:
    """Distinct client IPs seen in ask_log, optionally only within the trailing
    window. Excludes the '' / 'unknown' placeholder _client_ip() falls back to
    when there's no usable X-Forwarded-For, so it counts identifiable clients
    only. Spoofable and NAT-collapsed - a rough floor, not analytics."""
    sql = ("SELECT COUNT(DISTINCT client_ip) AS n FROM ask_log "
           "WHERE client_ip IS NOT NULL AND client_ip NOT IN ('', 'unknown')")
    params: list = []
    cutoff = _cutoff(since)
    if cutoff is not None:
        sql += " AND ts >= ?"
        params.append(cutoff)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def outcome_counts(conn: db.Connection, since: "timedelta | None" = None) -> dict:
    """{outcome: count} over ask_log, optionally windowed. Outcomes with no
    rows are simply absent - callers use .get(name, 0)."""
    sql = "SELECT outcome, COUNT(*) AS n FROM ask_log"
    params: list = []
    cutoff = _cutoff(since)
    if cutoff is not None:
        sql += " WHERE ts >= ?"
        params.append(cutoff)
    sql += " GROUP BY outcome"
    return {r["outcome"]: int(r["n"]) for r in conn.execute(sql, params).fetchall()}


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
        "WHERE client_ip IS NOT NULL AND client_ip NOT IN ('', 'unknown') "
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
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
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


def summary(conn: db.Connection) -> dict:
    """Everything the dashboard's stat tiles need, in one call. Any DB error
    yields a zeroed dict rather than propagating - a slow Neon wake must not
    500 the admin page."""
    try:
        return {
            "unique_24h": unique_clients(conn, timedelta(days=1)),
            "unique_7d": unique_clients(conn, timedelta(days=7)),
            "unique_all": unique_clients(conn, None),
            "questions_24h": _count_since(conn, timedelta(days=1)),
            "questions_7d": _count_since(conn, timedelta(days=7)),
            "outcomes_24h": outcome_counts(conn, timedelta(days=1)),
            "outcomes_all": outcome_counts(conn, None),
        }
    except Exception as exc:  # noqa: BLE001 - dashboard must render regardless
        print(f"[ask_log.summary] failed: {exc!r}", flush=True)
        return {
            "unique_24h": 0, "unique_7d": 0, "unique_all": 0,
            "questions_24h": 0, "questions_7d": 0,
            "outcomes_24h": {}, "outcomes_all": {},
        }
