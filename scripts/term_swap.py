"""Replace one term's sections with its successor's, subject by subject.

    python scripts/term_swap.py --to "summer 2027"                  # from summer 2026
    python scripts/term_swap.py --to "spring 2028" --from "fall 2027"
    python scripts/term_swap.py --to "summer 2027" --drop-rest      # after the run

For each subject that still has --from rows (largest first):
  1. run app.scraper for --to, that one subject (1 worker, 1 s between
     section requests - UIUC's AWS firewall challenged 3 and 10 workers)
  2. on a clean run (exit 0, no warnings) whose --to section count is at
     least half the --from count, delete the subject's --from sections and
     meetings in one transaction. Anything else keeps the --from rows, so the
     live site never shows a gap; the next run retries those subjects.

Stops at once if UIUC hasn't published --to yet (exit 3) or its firewall
starts challenging (exit 2). Subjects with no --to courses at all (retired,
or not offered that term) keep their --from rows; --drop-rest deletes every
remaining --from row once you've checked the list it prints.

Progress goes to scripts/progress-<to>.txt, per-subject logs to
scripts/logs/<to>/. Run from the project root with the venv's python;
scripts/watch_term_swap.py runs this detached and retries after blocks.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import terms

load_dotenv(ROOT / ".env")
HERE = Path(__file__).resolve().parent

EXIT_DONE, EXIT_ERROR, EXIT_BLOCKED, EXIT_NOT_PUBLISHED = 0, 1, 2, 3


def slug(year: int, sem: str) -> str:
    return f"{sem}-{year}"


def conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def remaining(old) -> list[tuple[str, int]]:
    """Subjects still holding --from rows, largest first."""
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT subject, COUNT(*) FROM sections WHERE year=%s AND semester=%s "
                    "GROUP BY 1 ORDER BY 2 DESC, 1", old)
        return [(r[0].strip(), r[1]) for r in cur.fetchall()]


def swap(subj: str, old, new) -> tuple[int, int, int]:
    """(new rows, old rows, old rows deleted). Deletes only when the new term
    looks complete - the scraper used to skip 404s silently, so a 'clean'
    run alone wasn't proof."""
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM sections WHERE year=%s AND semester=%s AND subject=%s",
                    (*new, subj))
        n_new = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sections WHERE year=%s AND semester=%s AND subject=%s",
                    (*old, subj))
        n_old = cur.fetchone()[0]
        if n_new == 0 or n_new < 0.5 * n_old:
            return n_new, n_old, 0
        cur.execute("DELETE FROM meetings WHERE year=%s AND semester=%s AND subject=%s", (*old, subj))
        cur.execute("DELETE FROM sections WHERE year=%s AND semester=%s AND subject=%s", (*old, subj))
        deleted = cur.rowcount
        c.commit()
        return n_new, n_old, deleted
    finally:
        c.close()


def drop_rest(old) -> int:
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM meetings WHERE year=%s AND semester=%s", old)
        cur.execute("DELETE FROM sections WHERE year=%s AND semester=%s", old)
        n = cur.rowcount
        c.commit()
        return n
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="the new term, e.g. 'summer 2027'")
    ap.add_argument("--from", dest="old", help="the term it replaces (default: same season, a year earlier)")
    ap.add_argument("--first", default="", help="comma-separated subjects to do before the rest")
    ap.add_argument("--pause", type=float, default=60, help="seconds between subjects (default 60)")
    ap.add_argument("--drop-rest", action="store_true",
                    help="delete every --from row still left (no scraping), then exit")
    args = ap.parse_args()

    new = terms.parse_term(args.to)
    old = terms.parse_term(args.old) if args.old else (new[0] - 1, new[1])
    progress = HERE / f"progress-{slug(*new)}.txt"
    logdir = HERE / "logs" / slug(*new)
    logdir.mkdir(parents=True, exist_ok=True)

    def log(line: str) -> None:
        print(line, flush=True)
        with progress.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    if args.drop_rest:
        left = remaining(old)
        n = drop_rest(old)
        log(f"== drop-rest {time.strftime('%Y-%m-%d %H:%M')}: deleted {n} {terms.term_label(*old)} "
            f"sections ({', '.join(s for s, _ in left) or 'none left'})")
        return EXIT_DONE

    todo = remaining(old)
    first = [s.strip().upper() for s in args.first.split(",") if s.strip()]
    order = [s for s in first if s in dict(todo)] + [s for s, _ in todo if s not in first]
    log(f"== start {time.strftime('%Y-%m-%d %H:%M')} {terms.term_label(*old)} -> "
        f"{terms.term_label(*new)}: {len(order)} subjects to do")
    env = dict(os.environ, HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    no_courses = []
    for subj in order:
        logfile = logdir / f"{subj}.log"
        with logfile.open("w", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, "-m", "app.scraper", "--year", str(new[0]),
                                 "--semester", new[1], "--subjects", subj,
                                 "--concurrency", "1", "--section-delay", "1.0"],
                                cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env, check=False).returncode
        text = logfile.read_text(encoding="utf-8", errors="replace")
        if rc == EXIT_NOT_PUBLISHED:
            log(f"== stopping: UIUC hasn't published {terms.term_label(*new)} yet.")
            return EXIT_NOT_PUBLISHED
        warns = text.count("[warn]")
        if "no courses found for" in text:
            no_courses.append(subj)
        clean = rc == 0 and warns == 0
        n_new, n_old, deleted = swap(subj, old, new) if clean else (None, None, 0)
        flag = ""
        if rc == EXIT_BLOCKED:
            flag = " BLOCKED"
        elif clean and not deleted:
            flag = " KEPT-OLD (new term looks small - check)"
        log(f"{subj} rc={rc} warns={warns} new={n_new} old={n_old} old_deleted={deleted}{flag}")
        if rc == EXIT_BLOCKED:
            log("== stopping: UIUC's firewall is challenging requests. Retry later.")
            return EXIT_BLOCKED
        time.sleep(args.pause)
    left = remaining(old)
    log(f"== done {time.strftime('%Y-%m-%d %H:%M')}; {len(left)} subject(s) still on "
        f"{terms.term_label(*old)}: {', '.join(s for s, _ in left) or 'none'}"
        + (f" (no {terms.term_label(*new)} courses: {', '.join(no_courses)})" if no_courses else ""))
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
