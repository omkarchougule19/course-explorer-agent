"""Keep scripts/term_swap.py going until the swap finishes.

Same arguments as term_swap.py (e.g. --to "summer 2027"). Re-runs it after a
firewall block (waits 30 min), and after "not published yet" (waits 24 h), and
exits once a run finishes. Meant to run detached, so neither a closed terminal
nor a tool's memory cleanup kills it:

    Start-Process .venv\\Scripts\\pythonw.exe -ArgumentList 'scripts\\watch_term_swap.py','--to','summer 2027' -WindowStyle Hidden

Stop it with:  Get-Content scripts\\watcher.pid | % { Stop-Process -Id $_ }
(then any leftover app.scraper / term_swap python processes).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG = HERE / "watcher.txt"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
WAIT = {2: 1800, 3: 24 * 3600}  # blocked: 30 min; not published: a day


def log(line: str) -> None:
    with LOG.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {line}\n")


def main() -> int:
    args = sys.argv[1:]
    (HERE / "watcher.pid").write_text(str(os.getpid()))
    log(f"watcher started (pid {os.getpid()}): term_swap {' '.join(args)}")
    while True:
        with LOG.open("a", encoding="utf-8") as f:
            rc = subprocess.run([str(PY if PY.exists() else sys.executable),
                                 str(HERE / "term_swap.py"), *args], cwd=ROOT,
                                stdout=f, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False).returncode
        if rc not in WAIT:
            log(f"term_swap exited {rc} - watcher done")
            return rc
        log(f"term_swap exited {rc}; retrying in {WAIT[rc] // 60} min")
        time.sleep(WAIT[rc])


if __name__ == "__main__":
    sys.exit(main())
