"""Keep the Spring 2027 rescrape going until every subject is swapped.

Runs scripts/rescrape_spring27.py whenever UIUC's API answers normally; when the
script stops (WAF block), waits 30 minutes and checks again. Exits when the
script logs "== done". Meant to run detached from any terminal:

    Start-Process .venv\\Scripts\\pythonw.exe scripts\\watch_rescrape.py -WindowStyle Hidden

Stop it with:  Get-Content scripts\\watcher.pid | % { Stop-Process -Id $_ }
(then stop any running app.scraper / rescrape_spring27 python processes).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
ROOT = HERE.parent
PROGRESS = HERE / "progress.txt"
LOG = HERE / "watcher.txt"
URL = "https://courses.illinois.edu/cisapp/explorer/schedule/2027/spring.xml"
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def log(line: str) -> None:
    with LOG.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{time.strftime('%H:%M')} {line}\n")


def done() -> bool:
    try:
        lines = PROGRESS.read_text(encoding="utf-8").strip().splitlines()
        return bool(lines) and lines[-1].startswith("== done")
    except OSError:
        return False


def uiuc_status() -> int:
    try:
        return requests.get(URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).status_code
    except requests.RequestException:
        return 0


def main() -> None:
    (HERE / "watcher.pid").write_text(str(os.getpid()))
    log(f"watcher started (pid {os.getpid()})")
    while not done():
        code = uiuc_status()
        log(f"uiuc={code}")
        if code == 200:
            with LOG.open("a", encoding="utf-8") as f:
                subprocess.run([str(PY), str(HERE / "rescrape_spring27.py")], cwd=ROOT,
                               stdout=f, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            continue
        time.sleep(1800)
    log("ALL SUBJECTS DONE - watcher exiting")


if __name__ == "__main__":
    main()
