"""
site_feedback.py

Stores free-text, site-wide feedback left through the box in the page footer -
"the search is confusing", "add grade data for LAS", bug reports, anything.
Distinct from app/feedback.py, which records a 👍/👎 tied to one assistant
answer; this has no question, no answer, no vote, just a message.

Table `site_feedback`:
    id, ts (UTC ISO), client_ip, message, page, reviewed_at

`page` is the browser path the box was submitted from (e.g. "/", "/admin.html")
so the operator knows where a complaint came from. `reviewed_at` stays NULL
until an operator marks the row handled (POST /admin/site-feedback/{id}/reviewed
in api.py).

There is no natural de-dupe key for free text, so abuse is bounded two ways,
both in `record()`:
    - the message is length-capped (SITE_FEEDBACK_MAX_CHARS, default 2000);
    - a per-IP daily cap (SITE_FEEDBACK_PER_IP_DAY, default 5) rejects further
      submissions from the same client IP in the trailing 24 h.
The IP is the spoofable first X-Forwarded-For hop, so the cap is friction, not
a control - it just stops an idle browser tab from filling the table.

Writes have no auth. Reads are via GET /admin/site-feedback, gated on
ADMIN_TOKEN.
"""

import os
from datetime import datetime, timedelta, timezone

from app import db

_MSG_MAX = int(os.environ.get("SITE_FEEDBACK_MAX_CHARS", "2000"))
_PAGE_MAX = 300
_PER_IP_DAY = int(os.environ.get("SITE_FEEDBACK_PER_IP_DAY", "5"))


def init_table(conn: db.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS site_feedback (
            id {db.autoincrement_pk()},
            ts TEXT NOT NULL,
            client_ip TEXT,
            message TEXT NOT NULL,
            page TEXT,
            reviewed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_feedback_ts ON site_feedback(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_feedback_ip ON site_feedback(client_ip, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_feedback_review "
        "ON site_feedback(reviewed_at)"
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _over_ip_cap(conn: db.Connection, client_ip: str) -> bool:
    """True once this IP has left SITE_FEEDBACK_PER_IP_DAY rows in the trailing
    24 h. Skipped when the IP is the '' / 'unknown' placeholder - there's
    nothing to attribute the count to."""
    if not client_ip or client_ip == "unknown":
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM site_feedback WHERE client_ip = ? AND ts >= ?",
        (client_ip, cutoff),
    ).fetchone()
    return (int(row["n"]) if row else 0) >= _PER_IP_DAY


def record(conn: db.Connection, client_ip: str, message: str,
           page: "str | None" = None) -> str:
    """Insert one feedback row. Returns a status string:
        "ok"            - written
        "empty"         - message was blank
        "too_long"      - message exceeded SITE_FEEDBACK_MAX_CHARS
        "rate_limited"  - this IP is over its daily cap
    The message is stored trimmed; `page` is clipped, never rejected."""
    message = (message or "").strip()
    if not message:
        return "empty"
    if len(message) > _MSG_MAX:
        return "too_long"
    if _over_ip_cap(conn, client_ip):
        return "rate_limited"

    page = (page or "").strip()[:_PAGE_MAX] or None
    conn.execute(
        "INSERT INTO site_feedback (ts, client_ip, message, page, reviewed_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (_now(), client_ip, message, page),
    )
    conn.commit()
    return "ok"


def recent(conn: db.Connection, reviewed: "bool | None" = None,
           limit: int = 100) -> list:
    """Feedback rows, newest first. `reviewed` is tri-state: None = all,
    False = not yet reviewed, True = reviewed."""
    limit = max(1, min(int(limit), 1000))
    where = ""
    if reviewed is True:
        where = " WHERE reviewed_at IS NOT NULL"
    elif reviewed is False:
        where = " WHERE reviewed_at IS NULL"
    rows = conn.execute(
        "SELECT id, ts, client_ip, message, page, reviewed_at "
        "FROM site_feedback" + where + " ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_reviewed(conn: db.Connection, feedback_id: int) -> bool:
    """Stamp reviewed_at on one row. Returns True if this call set it, False
    if the id doesn't exist or was already reviewed."""
    cur = conn.execute(
        "UPDATE site_feedback SET reviewed_at = ? WHERE id = ? AND reviewed_at IS NULL",
        (_now(), feedback_id),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


def counts(conn: db.Connection) -> dict:
    """{total, unreviewed} for the dashboard's feedback tile. Fails soft to
    zeroes so a slow Neon wake can't 500 the stats endpoint."""
    out = {"total": 0, "unreviewed": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN reviewed_at IS NULL THEN 1 ELSE 0 END) AS unreviewed "
            "FROM site_feedback"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - the tile is optional
        print(f"[site_feedback.counts] failed: {exc!r}", flush=True)
        return out
    if row:
        out["total"] = int(row["total"] or 0)
        out["unreviewed"] = int(row["unreviewed"] or 0)
    return out
