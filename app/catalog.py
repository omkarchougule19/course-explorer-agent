"""
catalog.py

Every lookup the website and the MCP server answer from: one course's facts,
a department's courses, section search, course search, the academic calendar
and the department list. Each function runs fixed, parameterized SQL on a
read-only connection and returns plain text. The site's assistant (app/agent.py)
and the MCP server (app/mcp_server.py) both call them; nothing here imports
the agent stack, so the MCP endpoint stays light.
"""

import os
import re
from functools import lru_cache

from app import db


def site_url() -> str:
    """Public address of this site, for links in MCP tool results, which are
    read outside the site and so need the host (no trailing slash).
    PUBLIC_BASE_URL if set, else the address Render gives the service. Empty
    locally, which leaves links site-relative."""
    return (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


_SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
_COURSE_ARG_RE = re.compile(r"\b([A-Za-z]{2,4})\s*-?\s*(\d{3}[A-Za-z]?)\b")
_FACTS_TERMS = 3        # recent terms listed per course
_FACTS_DESC_CHARS = 500


@lru_cache(maxsize=1)
def _subject_names() -> dict:
    """{code: name} for every department in the subjects table. Cached per
    process; {} when the table can't be read."""
    try:
        conn = db.get_readonly_connection()
        try:
            return {r["code"]: (r["name"] or "") for r in
                    conn.execute("SELECT code, name FROM subjects").fetchall() if r["code"]}
        finally:
            conn.close()
    except Exception:
        return {}


def _close_subjects(word: str, limit: int = 4) -> list:
    """The department codes a mistyped code most likely meant, as 'BADM
    (Business Administration)': codes it is the start of or that start it
    ('BAD' -> BADM, 'CSE' -> CS), then near spellings ('PYSC' -> PSYC)."""
    import difflib
    names = _subject_names()
    w = re.sub(r"[^A-Za-z]", "", word or "").upper()
    if not w or not names:
        return []
    found = [c for c in sorted(names) if c != w and (c.startswith(w) or w.startswith(c))]
    found += difflib.get_close_matches(w, list(names), n=limit, cutoff=0.7)
    return [f"{c} ({names[c]})" if names[c] else c for c in dict.fromkeys(found) if c != w][:limit]


def _resolve_subjects(conn, subjects: str):
    """Subject codes from what the model passed: real codes are kept; any
    other word ("agriculture", "media", or an invented code like 'AGRI') is
    looked up in subjects.search_text. Returns (codes, words that matched
    nothing). An agriculture search once passed AGRI, CULT and URE - none of
    them codes - and found nothing."""
    tokens = [t for t in re.split(r"[,;/]+|\s+and\s+", subjects or "") if t.strip()]
    if not tokens:
        return [], []
    try:
        known = {r["code"] for r in conn.execute("SELECT code FROM subjects").fetchall()}
    except Exception:
        known = set()
    codes, unresolved = [], []
    for tok in tokens:
        t = tok.strip()
        if t.upper() in known or (not known and re.fullmatch(r"[A-Za-z]{2,4}", t)):
            codes.append(t.upper())
            continue
        rows = []
        if known:
            rows = conn.execute("SELECT code FROM subjects WHERE search_text LIKE ? ORDER BY code",
                                (f"%{t.lower()}%",)).fetchall()
            if not rows and len(t) > 4:
                # "agriculture" vs "agricultural": try the word's stem
                rows = conn.execute("SELECT code FROM subjects WHERE search_text LIKE ? ORDER BY code",
                                    (f"%{t.lower()[:-3]}%",)).fetchall()
        if rows:
            codes += [r["code"] for r in rows]
        else:
            unresolved.append(t)
    return list(dict.fromkeys(codes)), unresolved


def course_facts_text(course: str, completed: str = "") -> str:
    """Everything the data holds about one named course, as a short fixed
    block (~500-900 chars): the title from course_label, description,
    credits, prerequisite groups already joined with "or", gen-eds, the last
    few terms' sections / instructors / status, and grades and Ranked
    Excellent rows when those tables have any. Fixed parameterized SQL on the
    read-only connection, so it can't invent a title (a run once called
    CS 444 "Computer Architecture") or forget the facts a should-I / is-it-hard
    / can-I question turns on. `completed` (courses the student says they've
    taken) adds a deterministic check of which prerequisite lines are met and
    which are missing: left to the model, one run told a student with CS 225
    and MATH 241 "you can take CS 444" while two lines were still missing."""
    m = _COURSE_ARG_RE.search(course or "")
    if not m:
        return "Give one course code, e.g. 'CS 444'."
    subj, num = m.group(1).upper(), m.group(2).upper()
    conn = db.get_readonly_connection()
    try:
        secs = conn.execute(
            "SELECT year, semester, instructor, enrollment_status, course_label, "
            "credit_hours, description, grad_credit, grad_min, grad_max, restriction "
            "FROM sections WHERE subject = ? AND course_number = ?",
            (subj, num)).fetchall()
        prereqs = conn.execute(
            "SELECT group_index, relation, req_subject, req_course_number, condition_text "
            "FROM prerequisites WHERE subject = ? AND course_number = ? ORDER BY group_index",
            (subj, num)).fetchall()
        geneds = conn.execute(
            "SELECT course_title, acp, cs, hum, nat, qr, sbs FROM gen_ed_categories "
            "WHERE subject = ? AND course_number = ?", (subj, num)).fetchall()
        grades = conn.execute(
            "SELECT primary_instructor, MIN(year) AS y0, MAX(year) AS y1, "
            "SUM(COALESCE(students, 0)) AS n, "
            "SUM(COALESCE(a_plus, 0) + COALESCE(a, 0) + COALESCE(a_minus, 0)) AS a_n "
            "FROM grade_distributions WHERE subject = ? AND course_number = ? "
            "GROUP BY primary_instructor", (subj, num)).fetchall()
        # For "should I drop it": the deadlines come with the course facts, so
        # an assistant doesn't need a second call to mention them.
        deadlines = conn.execute(
            "SELECT year, semester, event_date, title FROM academic_calendar "
            "WHERE category IN ('drop', 'withdraw') ORDER BY event_date").fetchall()
        instructors = sorted({r["instructor"] for r in secs
                              if r["instructor"] and r["instructor"].strip() not in ("", "-")})
        excellent = []
        for name in instructors[:6]:
            last, _, first = name.partition(",")
            rows = conn.execute(
                "SELECT DISTINCT year, term FROM teachers_ranked_excellent "
                "WHERE LOWER(last_name) = LOWER(?) AND UPPER(first_name) LIKE ?",
                (last.strip(), first.strip()[:1].upper() + "%")).fetchall()
            if rows:
                excellent.append(f"{name} ({', '.join(f'{r['term']} {r['year']}' for r in rows[:4])})")
    finally:
        conn.close()

    if not (secs or prereqs or geneds):
        return f"No course {subj} {num} in the data (no sections, prerequisites or gen-ed rows)."

    def term_key(r):
        return (r["year"] or 0, _SEASON_ORDER.get(r["semester"], 9))

    newest = max(secs, key=term_key) if secs else None
    title = (newest["course_label"] if newest else None) or (geneds[0]["course_title"] if geneds else "")
    out = [f"{subj} {num}: {title or '(no title in the data)'}"]
    if newest and newest["credit_hours"]:
        out.append(f"Credits: {newest['credit_hours']}")
    if newest:
        g = newest["grad_credit"]
        span = lambda lo, hi: f"{lo:g}" if lo == hi else f"{lo:g}-{hi:g}"
        out.append("Graduate credit: " + (
            f"yes, {span(newest['grad_min'], newest['grad_max'])} hours"
            if g == "yes" and newest["grad_min"] is not None
            else "yes" if g == "yes" else "none" if g == "no" else "not stated in the catalog"))
        if newest["restriction"]:
            out.append(f"Restriction: {newest['restriction']}")
    desc = next((r["description"] for r in sorted(secs, key=term_key, reverse=True)
                 if r["description"]), None)
    if desc:
        out.append("Description: " + (desc if len(desc) <= _FACTS_DESC_CHARS
                                      else desc[:_FACTS_DESC_CHARS].rsplit(" ", 1)[0] + "..."))
    else:
        out.append("Description: none scraped.")

    if prereqs:
        groups: dict = {}
        for r in prereqs:
            if r["req_subject"]:
                item = f"{r['req_subject']} {r['req_course_number']}"
                if r["relation"] == "concurrent":
                    item += " (may be taken concurrently)"
            else:
                item = r["condition_text"] or ""
            if item:
                groups.setdefault(r["group_index"], []).append(item)
        out.append("Prerequisites (every line required; 'or' = any one): "
                   + "; ".join(" or ".join(g) for g in groups.values()))
        taken = {f"{s.upper()} {n.upper()}" for s, n in _COURSE_ARG_RE.findall(completed or "")}
        if taken:
            met, missing, unchecked = [], [], []
            for r_group in groups.values():
                courses = [i.split(" (")[0] for i in r_group if _COURSE_ARG_RE.fullmatch(i.split(" (")[0])]
                if not courses:
                    unchecked.append(" or ".join(r_group))
                elif any(c in taken for c in courses):
                    met.append(next(c for c in courses if c in taken))
                else:
                    missing.append(" or ".join(r_group))
            verdict = ("all listed course prerequisites met" if not missing
                       else f"NOT yet eligible - {len(missing)} line(s) still missing")
            out.append(f"Check against the courses given ({', '.join(sorted(taken))}): {verdict}. "
                       f"Met: {'; '.join(met) or 'none'}. Missing: {'; '.join(missing) or 'none'}."
                       + (f" Not checkable from the data: {'; '.join(unchecked)}." if unchecked else ""))
    else:
        out.append("Prerequisites: none listed.")

    if geneds:
        codes = [g[k] for g in geneds for k in ("acp", "cs", "hum", "nat", "qr", "sbs") if g[k]]
        out.append("Gen-eds: " + (", ".join(dict.fromkeys(codes)) if codes else "none"))

    by_term: dict = {}
    for r in secs:
        by_term.setdefault((r["year"], r["semester"]), []).append(r)
    terms = sorted(by_term, key=lambda t: (t[0] or 0, _SEASON_ORDER.get(t[1], 9)), reverse=True)
    for year, sem in terms[:_FACTS_TERMS]:
        rows = by_term[(year, sem)]
        names = sorted({r["instructor"] for r in rows
                        if r["instructor"] and r["instructor"].strip() not in ("", "-")})
        statuses = [r["enrollment_status"] or "" for r in rows]
        if all(s in ("A", "P", "") for s in statuses):
            status = "registration not published yet"
        else:
            counts: dict = {}
            for s in statuses:
                counts[s or "unknown"] = counts.get(s or "unknown", 0) + 1
            status = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: -x[1]))
        shown = "; ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        out.append(f"{sem} {year}: {len(rows)} section(s); instructors: "
                   f"{shown or 'not assigned'}; status: {status}")
    if len(terms) > _FACTS_TERMS:
        out.append(f"(also offered in {len(terms) - _FACTS_TERMS} earlier term(s) in the data)")

    if grades:
        years = lambda g: str(g["y0"]) if g["y0"] == g["y1"] else f"{g['y0']}-{g['y1']}"
        parts = [f"{g['primary_instructor'] or 'unknown'} {years(g)}: "
                 f"{round(100 * (g['a_n'] or 0) / g['n'])}% A-range of {g['n']} students"
                 for g in grades if g["n"]]
        out.append("Grades: " + ("; ".join(parts) if parts else "none loaded"))
    else:
        out.append("Grades: none loaded for this course.")
    if excellent:
        out.append("Ranked Excellent by students: " + "; ".join(excellent))
    if deadlines:
        latest = max((r["year"] or 0, _SEASON_ORDER.get(r["semester"], 9)) for r in deadlines)
        rows = [r for r in deadlines
                if (r["year"] or 0, _SEASON_ORDER.get(r["semester"], 9)) == latest][:4]
        out.append(f"Drop/withdraw deadlines, {rows[0]['semester']} {rows[0]['year']}: "
                   + "; ".join(f"{r['event_date']} {r['title'][:90]}" for r in rows))
    out.append("Not in this data: workload, exam format, teaching quality beyond the "
               "above, degree/major rules, future offerings or seats.")
    return "\n".join(out)


_OVERVIEW_LIST_MAX = 80     # several departments: list every course up to this many in all
_OVERVIEW_TERM_RE = re.compile(r"\b(fall|spring|summer|winter)\s*(20\d\d)\b", re.IGNORECASE)


def department_overview_text(subjects: str, term: str = "", level: str = "", base: str = "") -> str:
    """What one or more departments offer, written as the finished answer
    (Markdown, courses linked): each course with its number of sections for
    one term - the term asked for, else the department's latest term with
    rows. One department is listed in full; several are listed while they
    total at most _OVERVIEW_LIST_MAX courses, and above that get a row of
    totals each and a prompt to pick one.

    Every branch is worded for the student, so an assistant can show it as
    is. Departments are given as codes or as words such as 'Gies'; a code
    that isn't one gets the closest real codes. `base` goes in front of each
    course link: empty for the site's own assistant (links stay relative),
    the site's address for answers shown elsewhere."""
    lvl = (re.search(r"[1-9]", level or "") or [None])[0]
    tm = _OVERVIEW_TERM_RE.search(term or "")
    asked = f"{tm.group(1).lower()} {tm.group(2)}" if tm else None
    conn = db.get_readonly_connection()
    try:
        codes, unresolved = _resolve_subjects(conn, subjects)
        if not codes:
            if not unresolved:
                return "Which department? Give a code like BADM or a name like Finance."
            close = list(dict.fromkeys(c for word in unresolved for c in _close_subjects(word)))
            named = ", ".join(f'"{w}"' for w in unresolved)
            if close:
                return (f"I couldn't find a department called {named}. Did you mean "
                        f"{' or '.join(close)}?")
            return (f"I couldn't find a department called {named}. Try its subject code "
                    "(like BADM) or a word from its name.")
        marks = ", ".join("?" for _ in codes)
        info = {r["code"]: r for r in conn.execute(
            f"SELECT code, name, college FROM subjects WHERE code IN ({marks})", codes).fetchall()}
        sql = ("SELECT subject, year, semester, course_number, MAX(course_label) AS title, "
               f"COUNT(*) AS n FROM sections WHERE subject IN ({marks}) AND year IS NOT NULL "
               "AND semester IS NOT NULL")
        params = list(codes)
        if lvl:
            sql += " AND course_number LIKE ?"
            params.append(f"{lvl}%")
        rows = conn.execute(sql + " GROUP BY subject, year, semester, course_number", params).fetchall()
    finally:
        conn.close()

    by_subject: dict = {}
    for r in rows:
        by_subject.setdefault(r["subject"], {}).setdefault(f"{r['semester']} {r['year']}", []).append(r)
    term_key = lambda t: (int(t.split()[1]), _SEASON_ORDER.get(t.split()[0], 9))
    chosen = {}
    for code in codes:
        terms = by_subject.get(code)
        if terms:
            chosen[code] = asked if asked in terms else max(terms, key=term_key)
    total = sum(len(by_subject[c][t]) for c, t in chosen.items())
    scope = f"{lvl}00-level " if lvl else ""
    plural = lambda n, word: f"{n:,} {word}{'' if n == 1 else 's'}"

    def full_name(code):
        row = info.get(code)
        return f"{code} ({row['name']})" if row is not None and row["name"] else code

    if not total:
        names = ", ".join(full_name(c) for c in codes)
        return (f"There's no data for that in this dataset yet. {names} has no {scope}sections "
                "in the data.")

    listing = len(chosen) == 1 or total <= _OVERVIEW_LIST_MAX
    out = []
    if not listing:
        out.append(f"That covers {plural(len(chosen), 'department')} and {plural(total, f'{scope}course')} - "
                   "too many to list at once. Here is each department's size:\n")
        out.append("| Department | Term | Courses | Sections |\n|---|---|---|---|")
    for code in codes:
        if code not in chosen:
            if listing:
                out.append(f"**{full_name(code)}** has no {scope}sections in the data.\n")
            else:
                out.append(f"| {full_name(code)} | - | 0 | 0 |")
            continue
        terms, shown = by_subject[code], chosen[code]
        courses = sorted(terms[shown], key=lambda r: r["course_number"])
        n_sections = sum(r["n"] for r in courses)
        if not listing:
            out.append(f"| {full_name(code)} | {shown} | {len(courses)} | {n_sections} |")
            continue
        row = info.get(code)
        college = f", {row['college']}" if row is not None and row["college"] and len(codes) == 1 else ""
        head = (f"**{full_name(code)}**{college}: {plural(n_sections, 'section')} across "
                f"{plural(len(courses), f'{scope}course')} in {shown}.")
        if asked and shown != asked:
            head = (f"There's no data for {code} in {asked} yet - that schedule hasn't been synced "
                    "(anyone can request it with Sync on the Departments page). "
                    f"**{full_name(code)}**{college} was last synced for {shown}: "
                    f"{plural(n_sections, 'section')} across {plural(len(courses), f'{scope}course')}.")
        out.append(head + "\n")
        out.append("| Course | Title | Sections |\n|---|---|---|")
        out += [f"| [{code} {r['course_number']}]({base}/?course={code}-{r['course_number']}) | "
                f"{(r['title'] or '').replace('|', '/')} | {r['n']} |" for r in courses]
        others = sorted((t for t in terms if t != shown), key=term_key, reverse=True)
        if others:
            out.append("\nAlso in the data: " + ", ".join(
                f"{t} ({plural(sum(r['n'] for r in terms[t]), 'section')})" for t in others[:4]) + ".")
        out.append("")
    first = next(iter(chosen))
    if listing:
        example = sorted(by_subject[first][chosen[first]], key=lambda r: -r["n"])[0]["course_number"]
        out.append(f"Ask about any course for its sections, times and instructors, for example "
                   f"\"{first} {example} sections\".")
    else:
        out.append(f"\nAsk for one department or a level to see its courses, for example "
                   f"\"{first} courses\" or \"{first} 400-level courses\".")
    return "\n".join(out).strip()


# --- searches ---------------------------------------------------------------------------------

_SEMESTERS = ("fall", "spring", "summer", "winter")
_TERM_RE = re.compile(r"\b(fall|spring|summer|winter)\s*(20\d\d)\b", re.IGNORECASE)
_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
MAX_ROWS = 60          # rows one search returns; the reply says when there were more


def _term(term: str):
    """('fall', 2026) from 'fall 2026' / 'Fall2026', or (None, None)."""
    m = _TERM_RE.search(term or "")
    return (m.group(1).lower(), int(m.group(2))) if m else (None, None)


def _minutes(hhmm: str):
    """Minutes after midnight from a 24-hour 'HH:MM', or None."""
    m = _HHMM_RE.match(hhmm or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def latest_term(conn, subject: str = ""):
    """The newest (semester, year) with sections - for one subject if given."""
    sql = "SELECT DISTINCT year, semester FROM sections WHERE year IS NOT NULL AND semester IS NOT NULL"
    params: list = []
    if subject:
        sql += " AND subject = ?"
        params.append(subject.upper())
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None, None
    best = max(rows, key=lambda r: (r["year"], _SEASON_ORDER.get(r["semester"], 9)))
    return best["semester"], best["year"]


def search_sections_text(subject: str = "", course_number: str = "", term: str = "", level: str = "",
                         instructor: str = "", starts_after: str = "", ends_before: str = "",
                         no_days: str = "", online_only: bool = False, limit: int = 40) -> str:
    """Sections matching the filters, one line each with CRN, section,
    instructor, status and every meeting (type, days, times, place). Needs a
    subject or an instructor. With no term, the subject's latest term is used
    and named in the reply. Time filters apply to a section's own meetings:
    a section passes only if none of its timed meetings breaks them."""
    subject = (subject or "").strip().upper()
    instructor = (instructor or "").strip()
    if not subject and not instructor:
        return "Give a subject code (for example 'CS') or an instructor's last name."
    limit = max(1, min(int(limit or 40), MAX_ROWS))
    lvl = (re.search(r"[1-9]", level or "") or [None])[0]
    t_after, t_before = _minutes(starts_after), _minutes(ends_before)
    avoid = {c for c in (no_days or "").upper() if c in "MTWRFSU"}

    conn = db.get_readonly_connection()
    try:
        if subject:
            names = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM subjects").fetchall()}
            if names and subject not in names:
                close = _close_subjects(subject)
                return (f"'{subject}' is not a department code."
                        + (f" Closest: {'; '.join(close)}." if close else " Use list_departments to find the code."))
        semester, year = _term(term)
        note = ""
        if not semester:
            semester, year = latest_term(conn, subject)
            if not semester:
                return f"No sections in the data for {subject or instructor}."
            note = f" (no term given: showing the latest term in the data, {semester} {year})"
        sql = ("SELECT subject, course_number, course_label, crn, section_name, instructor, "
               "enrollment_status, credit_hours FROM sections WHERE year = ? AND semester = ?")
        params: list = [year, semester]
        if subject:
            sql += " AND subject = ?"
            params.append(subject)
        if course_number.strip():
            sql += " AND course_number = ?"
            params.append(course_number.strip().upper())
        if lvl:
            sql += " AND course_number LIKE ?"
            params.append(f"{lvl}%")
        if instructor:
            from app.searchfields import normalize_last_name
            last = instructor.split(",")[0].split()[-1] if instructor.split(",")[0].split() else instructor
            sql += " AND instructor_last = ?"
            params.append(normalize_last_name(last))
        sql += " ORDER BY subject, course_number, crn"
        secs = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not secs:
            other = latest_term(conn, subject) if subject else (None, None)
            hint = (f" {subject}'s latest term in the data is {other[0]} {other[1]}."
                    if other[0] and (other[0], other[1]) != (semester, year) else "")
            return f"No sections match in {semester} {year}.{hint}"

        msql = ("SELECT subject, course_number, crn, meeting_type, days_of_week, start_time, end_time, "
                "building, room, start_min, end_min, is_online FROM meetings WHERE year = ? AND semester = ?")
        mparams: list = [year, semester]
        subjects = sorted({s["subject"] for s in secs})
        msql += " AND subject IN (" + ", ".join("?" for _ in subjects) + ")"
        mparams += subjects
        by_crn: dict = {}
        for m in conn.execute(msql, mparams).fetchall():
            by_crn.setdefault((m["subject"], m["course_number"], m["crn"]), []).append(dict(m))
    finally:
        conn.close()

    def fits(meets):
        if online_only and not any(m.get("is_online") for m in meets):
            return False
        for m in meets:
            if avoid and set((m.get("days_of_week") or "").upper()) & avoid:
                return False
            if t_after is not None and m.get("start_min") is not None and m["start_min"] < t_after:
                return False
            if t_before is not None and m.get("end_min") is not None and m["end_min"] > t_before:
                return False
        return True

    filtered = t_after is not None or t_before is not None or bool(avoid) or online_only
    out = []
    for s in secs:
        meets = by_crn.get((s["subject"], s["course_number"], s["crn"]), [])
        if filtered and not fits(meets):
            continue
        when = "; ".join(
            f"{m['meeting_type'] or 'Meeting'} {m['days_of_week'] or ''} "
            f"{(m['start_time'] or '').strip()}-{(m['end_time'] or '').strip()}"
            f"{' @ ' + ' '.join(x for x in (m['building'], m['room']) if x) if m['building'] else ''}".strip()
            for m in meets) or "no meeting times loaded"
        out.append(f"{s['subject']} {s['course_number']} {s['course_label'] or ''} | CRN {s['crn']}"
                   f"{' ' + s['section_name'] if s['section_name'] else ''} | "
                   f"{s['instructor'] or 'instructor not assigned'} | {s['enrollment_status'] or 'status unknown'} | {when}")
    if not out:
        return f"No sections match those filters in {semester} {year}{note}."
    head = f"{len(out)} section(s) in {semester} {year}{note}"
    if len(out) > limit:
        head += f"; showing the first {limit} - narrow by course_number, level or instructor to see the rest"
    return head + ":\n" + "\n".join(out[:limit]) + (
        "\nStatus 'A'/'P' is a scheduling code (registration not published yet), not open/closed."
        if any("| A |" in line or "| P |" in line for line in out[:limit]) else "")


def search_courses_text(query: str, subject: str = "", level: str = "", limit: int = 25) -> str:
    """Courses whose title or catalog description contains every word of the
    query, with a one-line description. Optional department and level."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) >= 2][:6]
    if not words:
        return "Give one or more words to search course titles and descriptions for."
    limit = max(1, min(int(limit or 25), MAX_ROWS))
    lvl = (re.search(r"[1-9]", level or "") or [None])[0]
    sql = ("SELECT subject, course_number, MAX(course_label) AS title, MAX(description) AS description "
           "FROM sections WHERE 1=1")
    params: list = []
    for w in words:
        sql += " AND (LOWER(course_label) LIKE ? OR title_search LIKE ? OR LOWER(description) LIKE ?)"
        params += [f"%{w}%"] * 3
    if subject.strip():
        sql += " AND subject = ?"
        params.append(subject.strip().upper())
    if lvl:
        sql += " AND course_number LIKE ?"
        params.append(f"{lvl}%")
    sql += " GROUP BY subject, course_number ORDER BY subject, course_number"
    conn = db.get_readonly_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No course title or description contains: {' '.join(words)}. Try fewer or different words."
    in_title = [r for r in rows if all(w in (r["title"] or "").lower() for w in words)]
    ordered = in_title + [r for r in rows if r not in in_title]
    lines = [f"{r['subject']} {r['course_number']} - {r['title'] or '(no title)'}: "
             f"{((r['description'] or 'no description')[:220]).rstrip()}" for r in ordered[:limit]]
    head = f"{len(rows)} course(s) match"
    if len(rows) > limit:
        head += f"; showing {limit} (title matches first) - add a subject or level to narrow"
    return head + ":\n" + "\n".join(lines)


def calendar_text(term: str = "", category: str = "") -> str:
    """Academic-calendar events for a term (default: the latest loaded),
    optionally one category: instruction, add, drop, withdraw, break,
    holiday, finals, grades, registration, commencement, other."""
    semester, year = _term(term)
    conn = db.get_readonly_connection()
    try:
        if not semester:
            rows = conn.execute("SELECT DISTINCT year, semester FROM academic_calendar").fetchall()
            if not rows:
                return "No academic calendar is loaded."
            best = max(rows, key=lambda r: (r["year"], _SEASON_ORDER.get(r["semester"], 9)))
            semester, year = best["semester"], best["year"]
        sql = ("SELECT event_date, event_end_date, title, category FROM academic_calendar "
               "WHERE year = ? AND semester = ?")
        params: list = [year, semester]
        if category.strip():
            cats = ["drop", "withdraw"] if category.strip().lower() in ("drop", "withdraw") else [category.strip().lower()]
            sql += " AND category IN (" + ", ".join("?" for _ in cats) + ")"
            params += cats
        events = conn.execute(sql + " ORDER BY event_date, title", params).fetchall()
    finally:
        conn.close()
    if not events:
        return f"No calendar events loaded for {semester} {year}" + (f" in category '{category}'." if category else ".")
    lines = [f"{e['event_date']}{' to ' + str(e['event_end_date']) if e['event_end_date'] else ''} "
             f"[{e['category']}] {e['title']}" for e in events[:MAX_ROWS]]
    return (f"Academic calendar, {semester} {year} ({len(events)} event(s)). Deadlines can differ by audience "
            "(undergraduate, graduate, Law, Vet Med) - the title says which:\n" + "\n".join(lines))


def departments_text(query: str = "") -> str:
    """Department codes with their names and colleges; `query` filters by any
    word of the code, name or college ('Gies', 'engineering', 'stat')."""
    q = (query or "").strip().lower()
    conn = db.get_readonly_connection()
    try:
        if q:
            rows = conn.execute("SELECT code, name, college FROM subjects WHERE search_text LIKE ? ORDER BY code",
                                (f"%{q}%",)).fetchall()
        else:
            rows = conn.execute("SELECT code, name, college FROM subjects ORDER BY code").fetchall()
    finally:
        conn.close()
    if not rows:
        close = _close_subjects(q)
        return f"No department matches '{query}'." + (f" Closest codes: {'; '.join(close)}." if close else "")
    if not q and len(rows) > 80:
        by_college: dict = {}
        for r in rows:
            by_college.setdefault(r["college"] or "Other", []).append(r["code"])
        return (f"{len(rows)} departments. By college:\n"
                + "\n".join(f"{c}: {', '.join(codes)}" for c, codes in sorted(by_college.items())))
    return f"{len(rows)} department(s):\n" + "\n".join(
        f"{r['code']} - {r['name']}{' (' + r['college'] + ')' if r['college'] else ''}" for r in rows)
