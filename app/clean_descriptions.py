"""
clean_descriptions.py

One-off backfill: strips embedded HTML markup (mainly <br/>) out of already-
scraped `sections.description` values. New scrapes are clean at the source
(see clean_description() in scraper.py, applied in fetch_course_description());
this script fixes rows written before that fix existed.

Safe to re-run - clean_description() is a no-op on already-clean text.

Usage:
    python app/clean_descriptions.py            # local SQLite (data/courses.db)
    DATABASE_URL=... python app/clean_descriptions.py   # prod Neon Postgres
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db
from app.scraper import clean_description


def main():
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT id, description FROM sections WHERE description LIKE '%<%'"
    ).fetchall()

    changed = 0
    for row in rows:
        cleaned = clean_description(row["description"])
        if cleaned != row["description"]:
            conn.execute("UPDATE sections SET description = ? WHERE id = ?", (cleaned, row["id"]))
            changed += 1

    conn.commit()
    conn.close()
    print(f"Scanned {len(rows)} rows with embedded markup, cleaned {changed}.")


if __name__ == "__main__":
    main()
