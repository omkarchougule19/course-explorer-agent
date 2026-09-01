"""
feedback.py

Records 👍/👎 votes on assistant answers so answer quality can be tracked and
the bad ones reviewed. Companion to ask_log.py.

Table `answer_feedback`:
    id, ts (UTC ISO), client_ip, vote ('up' | 'down'), question, answer,
    history_json, reviewed_at

Every vote writes a row. An **upvote** stores only the vote plus the Q/A text
(enough for an up/down ratio). A **downvote** additionally stores
`history_json` - a JSON snapshot of the windowed chat history the browser had
at the time - so a human can read the whole exchange during the biweekly
review pass. `reviewed_at` stays NULL until an operator marks that downvote
handled (GET/POST /admin/feedback* in api.py).

De-duped on (client_ip, question, answer): re-voting the same answer replaces
the previous row rather than piling up, which also lets a user flip 👍<->👎.
The answer text is supplied by the browser (there is no server-side answer
id); acceptable because the data only ever feeds a human review queue, every
field is length-capped here, and the dashboard HTML-escapes it on render.

Writes have no auth (it's just feedback). Reads are via GET /admin/feedback,
gated on the ADMIN_TOKEN env var.
"""

import json
from datetime import datetime, timezone

from app import db

# Caps on the browser-supplied text so a junk or abusive POST can't bloat the
# table or the review UI.
_Q_MAX = 1000
_A_MAX = 4000
_HISTORY_TURNS = 6
_HIST_A_MAX = 800

_VALID_VOTES = ("up", "down")


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS answer_feedback (
            id {db.autoincrement_pk()},
            ts TEXT NOT NULL,
            client_ip TEXT,
            vote TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            history_json TEXT,
            reviewed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_answer_feedback_ts ON answer_feedback(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_answer_feedback_review "
        "ON answer_feedback(vote, reviewed_at)"
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_history(history) -> "str | None":
    """Trim the browser's history list to the last few {q,a} turns, clip each
    field, and JSON-encode. Returns None when there's nothing usable. Clipping
    happens per field (not on the encoded blob) so the result is always valid
    JSON."""
    if not isinstance(history, list) or not history:
        return None
    turns = []
    for item in history[-_HISTORY_TURNS:]:
        if isinstance(item, dict):
            turns.append({
                "q": str(item.get("q", ""))[:_Q_MAX],
                "a": str(item.get("a", ""))[:_HIST_A_MAX],
            })
    return json.dumps(turns) if turns else None


def record(conn: db.Connection, client_ip: str, vote: str, question: str,
           answer: str, history=None) -> bool:
    """Insert (replacing any prior vote for the same client_ip + question +
    answer) one feedback row. Returns False if `vote` isn't 'up'/'down' or the
    question/answer is empty; True on a successful write. Downvotes keep the
    history snapshot; upvotes don't need it."""
    vote = (vote or "").strip().lower()
    question = (question or "").strip()[:_Q_MAX]
    answer = (answer or "").strip()[:_A_MAX]
    if vote not in _VALID_VOTES or not question or not answer:
        return False

    history_json = _clean_history(history) if vote == "down" else None

    # De-dupe by DELETE-then-INSERT rather than an upsert: the "same feedback"
    # key (client_ip, question, answer) isn't a table constraint - client_ip
    # legitimately repeats across different answers.
    conn.execute(
        "DELETE FROM answer_feedback WHERE client_ip = ? AND question = ? AND answer = ?",
        (client_ip, question, answer),
    )
    conn.execute(
        "INSERT INTO answer_feedback "
        "(ts, client_ip, vote, question, answer, history_json, reviewed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (_now(), client_ip, vote, question, answer, history_json),
    )
    conn.commit()
    return True


def recent(conn: db.Connection, vote: "str | None" = None,
           reviewed: "bool | None" = None, limit: int = 100) -> list:
    """Feedback rows, newest first. `vote` filters to 'up'/'down'. `reviewed`
    is tri-state: None = all, False = not yet reviewed, True = reviewed."""
    limit = max(1, min(int(limit), 1000))
    clauses: list = []
    params: list = []
    if vote in _VALID_VOTES:
        clauses.append("vote = ?")
        params.append(vote)
    if reviewed is True:
        clauses.append("reviewed_at IS NOT NULL")
    elif reviewed is False:
        clauses.append("reviewed_at IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT id, ts, client_ip, vote, question, answer, history_json, reviewed_at "
        "FROM answer_feedback" + where + " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def mark_reviewed(conn: db.Connection, feedback_id: int) -> bool:
    """Stamp reviewed_at on one row. Returns True if this call set it, False
    if the id doesn't exist or was already reviewed."""
    cur = conn.execute(
        "UPDATE answer_feedback SET reviewed_at = ? WHERE id = ? AND reviewed_at IS NULL",
        (_now(), feedback_id),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


def counts(conn: db.Connection) -> dict:
    """{up, down, down_unreviewed} for the dashboard's feedback tile."""
    rows = conn.execute(
        "SELECT vote, COUNT(*) AS n, "
        "SUM(CASE WHEN reviewed_at IS NULL THEN 1 ELSE 0 END) AS unreviewed "
        "FROM answer_feedback GROUP BY vote"
    ).fetchall()
    out = {"up": 0, "down": 0, "down_unreviewed": 0}
    for r in rows:
        if r["vote"] == "up":
            out["up"] = int(r["n"])
        elif r["vote"] == "down":
            out["down"] = int(r["n"])
            out["down_unreviewed"] = int(r["unreviewed"] or 0)
    return out
