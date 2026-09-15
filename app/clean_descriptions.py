"""
clean_descriptions.py

One-off backfill for dirty text already in `sections`, fixed at the source
for new scrapes but not retroactively for rows written before each fix
existed:

  - `description`: strips embedded HTML markup (mainly <br/>) - see
    clean_description() in scraper.py, applied in fetch_course_description().
  - `course_label`: HTML-unescapes double-encoded entities (e.g. a literal
    "&amp;" where the label should just contain "&") - see the html.unescape()
    call added in fetch_course_sections().

Safe to re-run - both fixes are no-ops on already-clean text.

Usage:
    python app/clean_descriptions.py            # local SQLite (data/courses.db)
    DATABASE_URL=... python app/clean_descriptions.py   # prod Neon Postgres
"""
import html
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app import db
from app.scraper import clean_description


def main():
    conn = db.get_connection()

    desc_rows = conn.execute(
        "SELECT id, description FROM sections WHERE description LIKE '%<%'"
    ).fetchall()
    desc_changed = 0
    for row in desc_rows:
        cleaned = clean_description(row["description"])
        if cleaned != row["description"]:
            conn.execute("UPDATE sections SET description = ? WHERE id = ?", (cleaned, row["id"]))
            desc_changed += 1

    label_rows = conn.execute(
        "SELECT id, course_label FROM sections WHERE course_label LIKE '%&amp;%'"
    ).fetchall()
    label_changed = 0
    for row in label_rows:
        cleaned = html.unescape(row["course_label"])
        if cleaned != row["course_label"]:
            conn.execute("UPDATE sections SET course_label = ? WHERE id = ?", (cleaned, row["id"]))
            label_changed += 1

    conn.commit()
    conn.close()
    print(f"description: scanned {len(desc_rows)} rows with embedded markup, cleaned {desc_changed}.")
    print(f"course_label: scanned {len(label_rows)} rows with a double-encoded entity, cleaned {label_changed}.")


if __name__ == "__main__":
    main()
