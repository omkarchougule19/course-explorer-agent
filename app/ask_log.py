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
