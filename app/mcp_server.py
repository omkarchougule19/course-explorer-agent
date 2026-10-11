"""
mcp_server.py

The MCP server: the course catalog as tools any MCP-capable assistant can
call, for students who would rather ask the assistant they already use than
the one on the site. No language model runs for these calls - the connecting
assistant does the reasoning and writes the answer; each tool is one fixed,
read-only lookup in app/catalog.py. Nothing here touches the site's own
assistant (app/agent.py) or its LLM budget.

Served over Streamable HTTP at /mcp by app/api.py (stateless, JSON replies),
so it runs on one small instance with nothing to keep between requests. To
try it on its own over stdio:

    python -m app.mcp_server
"""

import anyio
from mcp import types
from mcp.server.mcpserver import MCPServer

from app import catalog

INSTRUCTIONS = """\
Course data for the University of Illinois Urbana-Champaign (UIUC): sections, \
meeting times, instructors, prerequisites, gen-eds, the department list and the \
academic calendar. It is a periodic snapshot of UIUC Course Explorer, not a live \
registrar feed, and has no seat counts.

How to use it:
- A question about one course (what is it, prerequisites, who teaches it, can I \
take it) -> course_facts.
- What a department or college offers -> department_overview.
- Sections with times, days, rooms or instructors, or schedule constraints (no \
8 AMs, no Fridays, online) -> search_sections.
- Finding courses by topic or title words -> search_courses.
- Add/drop/withdraw deadlines, breaks, finals -> academic_calendar.
- Unsure of a department's code -> list_departments.

Things to tell the student: a term is a season plus a year ("fall 2026"); when a \
tool says a department has no rows for a term, that term simply isn't loaded for \
it yet - it does not mean nothing is offered. Instructors are stored as last \
name plus first initial, so a match may be a different person with the same \
initial. Status "A" or "P" means registration isn't published yet, not open or \
closed. Never present grades, workload or teaching quality as known: they are \
not in this data. For decisions, point to an academic advisor or the degree audit.\
"""

mcp = MCPServer(
    name="illini-course-catalog",
    title="Illini Course Catalog",
    description="Read-only lookups over a snapshot of the UIUC course catalog.",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)

_READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                   idempotent_hint=True, open_world_hint=False)


async def _lookup(fn, *args, **kwargs) -> str:
    """Run a blocking database lookup off the event loop, and turn a failure
    into a sentence the assistant can pass on. Driver errors can carry the
    host or role name, so they are logged, never returned."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        print(f"[mcp] {getattr(fn, '__name__', 'lookup')} failed: {exc!r}", flush=True)
        return "The course data is temporarily unavailable. Try again in a moment."


@mcp.tool(title="Course facts", annotations=_READ_ONLY)
async def course_facts(course: str, completed: str = "") -> str:
    """Everything the data holds about ONE course: title, description,
    credits, prerequisites (each line required; "or" = any one), gen-eds, the
    last few terms' sections, instructors and registration status, grade
    history if loaded, and the drop/withdraw deadlines.

    course: a course code, e.g. "CS 225" or "badm554".
    completed: for "can I take it" questions, the courses the student has
    taken, comma-separated ("CS 128, MATH 231"). The reply then says which
    prerequisite lines are met and which are missing.
    """
    return await _lookup(catalog.course_facts_text, course, completed)


@mcp.tool(title="Department overview", annotations=_READ_ONLY)
async def department_overview(subjects: str, term: str = "", level: str = "") -> str:
    """What a whole department or college offers: every course with its number
    of sections for one term, as a Markdown table with links. Several
    departments that are too many to list get a row of totals each.

    subjects: department code(s) ("BADM", "CS,ECE") or words as the student
    wrote them ("Gies", "agriculture") - words become every matching department.
    term: e.g. "fall 2026". Empty = each department's latest term in the data.
    level: one digit for a level ("4" = 400-level). Empty = all levels.
    """
    return await _lookup(catalog.department_overview_text, subjects, term, level,
                         catalog.site_url())


@mcp.tool(title="Search sections", annotations=_READ_ONLY)
async def search_sections(subject: str = "", course_number: str = "", term: str = "", level: str = "",
                          instructor: str = "", starts_after: str = "", ends_before: str = "",
                          no_days: str = "", online_only: bool = False, limit: int = 40) -> str:
    """Individual sections with CRN, instructor, registration status and every
    meeting (type, days, start-end time, building and room). Give a subject or
    an instructor; everything else narrows the list.

    subject: department code, e.g. "CS".
    course_number: e.g. "225".
    term: e.g. "fall 2026". Empty = the latest term in the data (the reply names it).
    level: one digit ("1" = 100-level, "5" = 500-level graduate).
    instructor: last name, or "Last, F".
    starts_after: 24-hour "HH:MM"; drops sections with any meeting starting
    earlier ("09:00" = no 8 AMs).
    ends_before: 24-hour "HH:MM"; drops sections with any meeting ending later
    ("17:00" = done by 5).
    no_days: day letters to avoid, from M T W R F S U (R = Thursday); "F" = no Fridays.
    online_only: only sections with an online meeting.
    limit: most sections to return (up to 60).
    """
    return await _lookup(catalog.search_sections_text, subject, course_number, term, level, instructor,
                         starts_after, ends_before, no_days, online_only, limit)


@mcp.tool(title="Search courses", annotations=_READ_ONLY)
async def search_courses(query: str, subject: str = "", level: str = "", limit: int = 25) -> str:
    """Find courses by words in the title or catalog description ("machine
    learning", "organic chemistry"). Every word must appear; title matches
    come first. Use a few plain words, and spell out abbreviations ("artificial
    intelligence", not "AI").

    query: the words to look for.
    subject: limit to one department code, e.g. "CS".
    level: one digit for a level ("4" = 400-level).
    limit: most courses to return (up to 60).
    """
    return await _lookup(catalog.search_courses_text, query, subject, level, limit)


@mcp.tool(title="Academic calendar", annotations=_READ_ONLY)
async def academic_calendar(term: str = "", category: str = "") -> str:
    """Registrar calendar events for a term: first and last day of instruction,
    add / drop / withdraw deadlines, breaks, holidays, finals, grade deadlines.

    term: e.g. "fall 2026". Empty = the latest term loaded.
    category: one of instruction, add, drop, withdraw, break, holiday, finals,
    grades, registration, commencement, other. "drop" also returns withdraw
    deadlines. Empty = everything.
    """
    return await _lookup(catalog.calendar_text, term, category)


@mcp.tool(title="List departments", annotations=_READ_ONLY)
async def list_departments(query: str = "") -> str:
    """Department (subject) codes with their names and colleges. Use it to turn
    a name into a code before another call.

    query: any word of the code, name or college ("Gies", "engineering",
    "stat"). Empty = every department, grouped by college.
    """
    return await _lookup(catalog.departments_text, query)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    mcp.run()
