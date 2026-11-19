"""Gentle Spring 2027 rescrape with a per-subject Spring 2026 swap.

For each subject that still has Spring 2026 rows (INFO first, then by size):
  1. run app.scraper for that one subject (1 worker, 1s between section
     requests, 60s between subjects - UIUC's AWS WAF blocked 3-worker runs)
  2. on a clean run (exit 0, no warnings), delete its Spring 2026 sections +
     meetings in one transaction; a partial run keeps them for the next try
Stops at the first sign of the WAF challenge (whole-subject refusal or 20+ warns).
Run from the project root:  .venv/Scripts/python <this file>
"""
import os, subprocess, sys, time
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

ROOT = Path.cwd()
load_dotenv(ROOT / ".env")
LOGDIR = Path(__file__).parent / "logs"
LOGDIR.mkdir(exist_ok=True)
PROGRESS = Path(__file__).parent / "progress.txt"


def conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def todo():
    with conn() as c, c.cursor() as cur:
        # Anything that still has Spring 2026 rows hasn't been swapped yet - including
        # subjects with a partial Spring 2027 load (e.g. one cut off by the WAF).
        cur.execute("""select subject, count(*) from sections where year=2026 and semester='spring'
                       group by 1 order by 2 desc""")
        subs = [r[0].strip() for r in cur.fetchall()]
    return (["INFO"] if "INFO" in subs else []) + [s for s in subs if s != "INFO"]


def swap(subj):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("select count(*) from sections where year=2027 and semester='spring' and subject=%s", (subj,))
        n27 = cur.fetchone()[0]
        cur.execute("select count(*) from sections where year=2026 and semester='spring' and subject=%s", (subj,))
        n26 = cur.fetchone()[0]
        # The scraper skips 404s without a warning, so a "clean" run can still be
        # missing courses. Keep Spring 2026 unless Spring 2027 looks complete.
        if n27 == 0 or n27 < 0.5 * n26:
            return n27, 0
        cur.execute("delete from meetings where year=2026 and semester='spring' and subject=%s", (subj,))
        cur.execute("delete from sections where year=2026 and semester='spring' and subject=%s", (subj,))
        deleted = cur.rowcount
        c.commit()
        return n27, deleted
    finally:
        c.close()


def log(line):
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def main():
    env = dict(os.environ, HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    subjects = todo()
    log(f"== start {time.strftime('%H:%M')} - {len(subjects)} subjects to do")
    for subj in subjects:
        logfile = LOGDIR / f"{subj}.log"
        with logfile.open("w", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, "-m", "app.scraper", "--year", "2027", "--semester", "spring",
                                 "--subjects", subj, "--concurrency", "1", "--section-delay", "1.0"],
                                stdout=f, stderr=subprocess.STDOUT, env=env).returncode
        text = logfile.read_text(encoding="utf-8", errors="replace")
        warns = text.count("[warn]")
        # Whole-subject refusal, or the WAF kicking in mid-subject (PHYS: 125 warns).
        blocked = "Can't reach the Course Explorer API" in text or warns >= 20
        # Swap only on a clean run - a partial scrape keeps its Spring 2026 rows.
        n27, deleted = swap(subj) if (rc == 0 and warns == 0) else (None, 0)
        small = n27 is not None and deleted == 0 and n27 > 0
        log(f"{subj} rc={rc} warns={warns} spring27={n27} spring26_deleted={deleted}"
            f"{' BLOCKED' if blocked else ''}{' KEPT-2026 (2027 looks small - check)' if small else ''}")
        if blocked:
            log("== stopping: UIUC WAF is blocking again. Retry later.")
            return
        time.sleep(60)
    log(f"== done {time.strftime('%H:%M')}")


if __name__ == "__main__":
    main()
