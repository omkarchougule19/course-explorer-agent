"""
catalog.py

The fixed lookups the assistant offers as tools: one course's facts
(course_facts) and a department's courses (department_overview). Each runs
fixed, parameterized SQL on a read-only connection and returns plain text.
Kept apart from app/agent.py so the lookups can be read and tested without
the agent stack.
"""

import re
from functools import lru_cache

from app import db


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


def department_overview_text(subjects: str, term: str = "", level: str = "") -> str:
    """What one or more departments offer, written as the finished answer
    (Markdown, courses linked): each course with its number of sections for
    one term - the term asked for, else the department's latest term with
    rows. One department is listed in full; several are listed while they
    total at most _OVERVIEW_LIST_MAX courses, and above that get a row of
    totals each and a prompt to pick one.

    Every branch is worded for the student, so an assistant can show it as
    is. Departments are given as codes or as words such as 'Gies'; a code
    that isn't one gets the closest real codes."""
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
        out += [f"| [{code} {r['course_number']}](/?course={code}-{r['course_number']}) | "
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
