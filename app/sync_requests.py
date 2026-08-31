"""
sync_requests.py

Demand-driven, department-level refresh for the deployed app.

Because the UIUC Course Explorer WAF soft-blocks a full-catalog scrape (after
a handful of subjects it returns HTTP 200 with empty course lists - see
DECISIONS.md), there is no scheduled full re-scrape. Instead:

  * The web UI shows each department's last-synced date and a "Sync" button
    (hidden when the department was refreshed in the last 7 days).
  * Clicking it calls POST /sync/request, which bumps a per-department counter
    in the `sync_requests` table. No login, no rate limit.
  * You, running locally on a residential IP, periodically look at which
    departments have the most pending requests and refresh those - stopping
    when the WAF starts soft-rejecting.

This module is both the table definition (imported by api.py) and that
operator CLI:

    python -m app.sync_requests --list                # departments by demand
    python -m app.sync_requests --run MECH ECE PHYS   # refresh these, in order
    python -m app.sync_requests --run --top 5         # refresh the 5 most-requested
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db  # noqa: E402  - after load_dotenv so DATABASE_URL is seen
from app import scraper  # noqa: E402
from app import terms  # noqa: E402

# A department someone synced a few days ago doesn't need re-fetching - course
# data barely moves week to week. Passed to scraper.run as skip_recent_hours.
RECENT_HOURS = 7 * 24

# Consecutive departments that had data on file but come back with zero
# courses now => the WAF has started soft-rejecting. Stop the run here.
WALL_STREAK = 2


def init_table(conn: db.Connection) -> None:
    """Create the sync_requests table if missing. Safe to call on every app
    startup and at the top of the CLI."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_requests (
            subject TEXT PRIMARY KEY,
            pending_count INTEGER NOT NULL DEFAULT 0,
            last_requested_at TEXT
        )
        """
    )
    conn.commit()


def record_request(conn: db.Connection, subject: str) -> int:
    """Bump a department's pending counter (POST /sync/request). Returns the
    new count.

    Read-modify-write rather than a SQL `pending_count = pending_count + 1`
    upsert, because the qualification needed to make that unambiguous differs
    between SQLite and Postgres. A lost increment under concurrent clicks is
    harmless here - the operator ranks by relative magnitude and there's no
    rate limit riding on the exact value."""
    subject = subject.strip().upper()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT pending_count FROM sync_requests WHERE subject = ?", (subject,)
    ).fetchone()
    if row:
        # Clamp: this is an unauthenticated counter with no rate limit, so cap
        # it well above any plausible real demand rather than let it be pumped
        # unboundedly.
        new_count = min(int(row["pending_count"]) + 1, 100_000)
        conn.execute(
            "UPDATE sync_requests SET pending_count = ?, last_requested_at = ? WHERE subject = ?",
            (new_count, now, subject),
        )
    else:
        new_count = 1
        conn.execute(
            "INSERT INTO sync_requests (subject, pending_count, last_requested_at) VALUES (?, ?, ?)",
            (subject, 1, now),
        )
    conn.commit()
    return new_count


def status(conn: db.Connection) -> list[dict]:
    """One row per department: pending request count, last-synced timestamp
    (newest scraped_at across its sections), and section count. Powers the UI
    department panel and the --list CLI. Ordered by demand, then name.

    Merged in Python from two simple GROUP BYs rather than a FULL OUTER JOIN
    (unsupported on SQLite) - and so a department that's been requested but
    isn't in `sections` yet still shows up."""
    by_subject: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT subject, COUNT(*) AS section_count, MAX(scraped_at) AS last_synced_at "
        "FROM sections GROUP BY subject"
    ).fetchall():
        by_subject[r["subject"]] = {
            "subject": r["subject"],
            "section_count": int(r["section_count"]),
            "last_synced_at": r["last_synced_at"],
            "pending_count": 0,
            "last_requested_at": None,
        }
    for r in conn.execute(
        "SELECT subject, pending_count, last_requested_at FROM sync_requests"
    ).fetchall():
        d = by_subject.setdefault(r["subject"], {
            "subject": r["subject"], "section_count": 0,
            "last_synced_at": None, "pending_count": 0, "last_requested_at": None,
        })
        d["pending_count"] = int(r["pending_count"])
        d["last_requested_at"] = r["last_requested_at"]
    return sorted(by_subject.values(), key=lambda d: (-d["pending_count"], d["subject"]))


# ---------------------------------------------------------------------------
# operator CLI
# ---------------------------------------------------------------------------

def _next_term(year: int, semester: str) -> tuple[int, str]:
    cyc = terms.SEMESTER_CYCLE
    i = year * 3 + cyc.index(semester) + 1
    y, si = divmod(i, 3)
    return y, cyc[si]


def _term_published(year: int, semester: str) -> bool:
    """Has UIUC published this term's schedule yet? One warmed request to the
    term-level XML - used to decide whether to also refresh the next term."""
    scraper.warmup(year, semester)
    return scraper.fetch_xml(f"{scraper.BASE_URL}/{year}/{semester}.xml") is not None


def _parse_ts(value) -> "datetime | None":
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:  # SQLite stores 'YYYY-MM-DD HH:MM:SS' (naive UTC)
        return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age(value) -> str:
    ts = _parse_ts(value)
    if ts is None:
        return "never"
    days = (datetime.now(timezone.utc) - ts).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def print_list(conn: db.Connection) -> int:
    rows = status(conn)
    pending = [r for r in rows if r["pending_count"] > 0]
    show = pending or rows
    header = "departments with pending sync requests" if pending else "all departments (none requested yet)"
    print(f"{header}\n")
    print(f"{'DEPT':<8}{'PENDING':>8}{'SECTIONS':>10}   LAST SYNCED")
    for r in show[:60]:
        print(f"{r['subject']:<8}{r['pending_count']:>8}{r['section_count']:>10}   {_age(r['last_synced_at'])}")
    if pending:
        print(f"\n{len(pending)} department(s) requested. "
              f"Refresh the hottest with:  python -m app.sync_requests --run "
              f"{' '.join(r['subject'] for r in pending[:5])}")
    return 0


def run_syncs(subjects: list[str], top: "int | None" = None) -> int:
    conn = db.get_connection()
    init_table(conn)
    if not db.is_postgres():
        print("Warning: DATABASE_URL not set - syncing into local SQLite, not Neon.\n", flush=True)

    if top:
        subjects = [r["subject"] for r in status(conn) if r["pending_count"] > 0][:top]

    subjects = [s.strip().upper() for s in subjects if s.strip()]
    if not subjects:
        print("Nothing to sync. Pass department codes, or --top N once there are "
              "pending requests (see --list).", flush=True)
        conn.close()
        return 1

    cur = (terms.CURRENT_YEAR, terms.CURRENT_SEMESTER)
    nxt = _next_term(*cur)
    nxt_ok = _term_published(*nxt)
    term_list = [cur] + ([nxt] if nxt_ok else [])
    print(f"Departments ({len(subjects)}): {', '.join(subjects)}")
    print(
        f"Terms: {cur[1]} {cur[0]}"
        + (f" + {nxt[1]} {nxt[0]}" if nxt_ok else f"  (next term {nxt[1]} {nxt[0]} not published - skipping)")
        + "\n",
        flush=True,
    )

    wall = 0
    done: list[str] = []
    blocked: list[str] = []
    for subj in subjects:
        prior = conn.execute(
            "SELECT COUNT(*) AS n FROM sections WHERE subject = ?", (subj,)
        ).fetchone()["n"]
        srow = conn.execute(
            "SELECT pending_count FROM sync_requests WHERE subject = ?", (subj,)
        ).fetchone()
        snapshot = int(srow["pending_count"]) if srow else 0

        soft_rejected = False
        for (y, s) in term_list:
            res = scraper.run(
                y, s, subjects=[subj], skip_recent_hours=RECENT_HOURS, quiet_errors=True
            )
            found = res["per_subject"].get(subj, {}).get("courses_found")
            if (y, s) == cur and prior > 0 and found == 0:
                soft_rejected = True

        if soft_rejected:
            wall += 1
            blocked.append(subj)
            print(f"  ! {subj}: current term returned 0 courses but {prior} sections "
                  f"are on file - soft-reject.", flush=True)
            if wall >= WALL_STREAK:
                print(f"\nStopping - {wall} departments in a row look soft-rejected. "
                      f"Wait a while and re-run.", flush=True)
                break
        else:
            wall = 0
            done.append(subj)
            # Subtract only the demand that existed when this dept's sync
            # started, so clicks that arrived mid-sync are preserved. Read the
            # count fresh rather than trusting `snapshot` for the write.
            cur_row = conn.execute(
                "SELECT pending_count FROM sync_requests WHERE subject = ?", (subj,)
            ).fetchone()
            current = int(cur_row["pending_count"]) if cur_row else 0
            conn.execute(
                "UPDATE sync_requests SET pending_count = ? WHERE subject = ?",
                (max(0, current - snapshot), subj),
            )
            conn.commit()

    conn.close()
    print(f"\nSynced:  {', '.join(done) or 'none'}")
    if blocked:
        print(f"Soft-rejected (retry later):  {', '.join(blocked)}")
    skipped = [s for s in subjects if s not in done and s not in blocked]
    if skipped:
        print(f"Not attempted:  {', '.join(skipped)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Department-level, demand-driven refresh (see module docstring)."
    )
    parser.add_argument("--list", action="store_true", help="show departments ranked by pending sync requests")
    parser.add_argument("--run", nargs="*", metavar="DEPT",
                        help="refresh these departments in order (or none, with --top)")
    parser.add_argument("--top", type=int, default=None, metavar="N",
                        help="with --run and no departments listed: refresh the N most-requested")
    args = parser.parse_args()

    conn = db.get_connection()
    init_table(conn)

    if args.run is not None:
        conn.close()
        return run_syncs(args.run, top=args.top)

    # default action is --list
    try:
        return print_list(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
