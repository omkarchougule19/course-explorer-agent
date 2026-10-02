"""
load_subjects.py

Loads `subjects(code, name, college_code, college)` from Course Explorer, so
a department or college named in words ("courses offered by Gies", "AI
courses in agriculture", "the information sciences school") resolves to
subject codes. Before this, the only mapping was the model's memory: "Gies"
got "no courses" (ACCY/BADM/FIN/BUS exist) and "agriculture" found only
AGCM/AGED.

The subject list (code -> name) comes from the term's schedule XML; each
subject's own XML gives its collegeCode. Course Explorer gives no college
names, so COLLEGES maps the codes, identified from their subjects and
websites on 2026-10-01 (e.g. KM: ACCY/BADM/FIN, giesbusiness.illinois.edu).
A code missing from the map is stored with college NULL.

    python -m app.load_subjects                 # every term in the data
    python -m app.load_subjects --year 2026 --semester fall
    python -m app.load_subjects --seed saved.json   # fill colleges from a saved fetch,
                                                    # no Course Explorer requests

search_text is "code name college" lowercased, so one LIKE finds a
department by code, name or college ("Gies" is only in the college name;
a search on name alone found nothing).
"""

import argparse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db  # noqa: E402

BASE = "https://courses.illinois.edu/cisapp/explorer/schedule"

# college code -> name as students say it (official name, plus the short
# names they use, so a LIKE on either finds it).
COLLEGES = {
    "KV": "College of Liberal Arts and Sciences (LAS)",
    "KP": "Grainger College of Engineering",
    "KL": "College of Agricultural, Consumer and Environmental Sciences (ACES, agriculture)",
    "KR": "College of Fine and Applied Arts (FAA)",
    "KN": "College of Education",
    "KM": "Gies College of Business",
    "KT": "College of Media",
    "KY": "College of Applied Health Sciences (AHS)",
    "LC": "College of Veterinary Medicine",
    "LD": "Military science / ROTC (Air Force, Army, Naval)",
    "LT": "Carle Illinois College of Medicine",
    "KS": "Graduate College",
    "LP": "School of Information Sciences (iSchool)",
    "LL": "School of Social Work",
    "KW": "Division of General Studies",
    "KU": "College of Law",
    "LG": "School of Labor and Employment Relations (LER)",
}


def _strip(tag: str) -> str:
    return tag.split("}")[-1]


def fetch_subjects(year: int, semester: str) -> list:
    """[(code, name, college_code or None, college or None)]. Uses the
    scraper's fetch_xml (retries with backoff): with plain requests, a
    throttled Course Explorer returned empty bodies and about 130 subjects
    lost their college."""
    from app.scraper import fetch_xml
    root = fetch_xml(f"{BASE}/{year}/{semester}.xml", retries=5)
    if root is None:
        return []
    pairs = [(el.get("id"), (el.text or "").strip()) for el in root.iter() if _strip(el.tag) == "subject"]

    def college(code: str):
        r = fetch_xml(f"{BASE}/{year}/{semester}/{code}.xml", retries=5)
        if r is None:
            return None
        return next(((e.text or "").strip() for e in r if _strip(e.tag) == "collegeCode"), None)

    with ThreadPoolExecutor(3) as ex:
        codes = list(ex.map(college, [c for c, _ in pairs]))
    return [(c, n, cc, COLLEGES.get(cc)) for (c, n), cc in zip(pairs, codes)]


def init_table(conn: db.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subjects (code TEXT PRIMARY KEY, name TEXT, "
        "college_code TEXT, college TEXT, search_text TEXT)"
    )
    if "search_text" not in db.existing_columns(conn, "subjects"):
        conn.execute("ALTER TABLE subjects ADD COLUMN search_text TEXT")
    conn.commit()


def search_text(code, name, college) -> str:
    return " ".join(x for x in (code, name, college) if x).lower()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int)
    ap.add_argument("--semester")
    ap.add_argument("--seed", help="JSON list of {code, name, college_code} from an earlier fetch; "
                                   "fills missing names/colleges without calling Course Explorer")
    args = ap.parse_args()
    conn = db.get_connection()
    try:
        if args.seed:
            import json
            init_table(conn)
            seed = {r["code"]: r for r in json.load(open(args.seed, encoding="utf-8")) if r.get("code")}
            stored = {r["code"]: r for r in conn.execute(
                "SELECT code, name, college_code, college FROM subjects").fetchall()}
            rows = []
            for code in sorted(set(seed) | set(stored)):
                old, new = stored.get(code), seed.get(code, {})
                name = (old["name"] if old and old["name"] else None) or new.get("name")
                cc = (old["college_code"] if old and old["college_code"] else None) or new.get("college_code")
                college = COLLEGES.get(cc)
                rows.append((code, name, cc, college, search_text(code, name, college)))
            db.upsert(conn, "subjects", ("code", "name", "college_code", "college", "search_text"),
                      rows, ("code",))
            conn.commit()
            print(f"{conn.backend}: {len(rows)} subjects after seeding; without a college: "
                  f"{[r[0] for r in rows if not r[3]] or 'none'}")
            return
        if args.year and args.semester:
            terms = [(args.year, args.semester)]
        else:
            # Every term in the data, oldest first, so the newest name wins;
            # a subject offered only in spring still gets a row (the fall
            # list alone had 186 of the 191 subjects in sections).
            order = {"spring": 1, "summer": 2, "fall": 3}
            terms = sorted({(r["year"], r["semester"]) for r in
                            conn.execute("SELECT DISTINCT year, semester FROM sections").fetchall()},
                           key=lambda t: (t[0], order.get(t[1], 0)))
        # Start from what's stored, so a fetch that fails now never erases a
        # college an earlier load found.
        init_table(conn)
        merged = {r["code"]: (r["code"], r["name"], r["college_code"], r["college"], r["search_text"])
                  for r in conn.execute("SELECT code, name, college_code, college, search_text "
                                        "FROM subjects").fetchall()}
        for year, semester in terms:
            for code, name, cc, college in fetch_subjects(year, semester):
                # A per-subject fetch can fail (cc None): keep the college an
                # earlier term found rather than overwrite it with nothing.
                if not cc and code in merged:
                    cc, college = merged[code][2], merged[code][3]
                merged[code] = (code, name, cc, college, search_text(code, name, college))
        rows = list(merged.values())
        init_table(conn)
        db.upsert(conn, "subjects", ("code", "name", "college_code", "college", "search_text"),
                  rows, ("code",))
        conn.commit()
        unmapped = sorted({r[2] for r in rows if r[2] and not r[3]})
        print(f"{conn.backend}: {len(rows)} subjects from {len(terms)} term(s); "
              f"college codes without a name: {unmapped or 'none'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
