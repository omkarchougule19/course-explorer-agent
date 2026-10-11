"""
End-to-end check of the MCP endpoint, as an assistant would use it: connect
over Streamable HTTP, list the tools, call each one. Needs the server running
and a database with data:

    uvicorn app.api:app --port 8765
    python -m evals.test_mcp_live                      # http://127.0.0.1:8765/mcp
    python -m evals.test_mcp_live https://<host>/mcp   # a deployed server
"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"
failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def text_of(result) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def main():
    async with streamable_http_client(URL) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            print("connected to:", getattr(info, "name", info), "| instructions:",
                  len(getattr(init, "instructions", "") or ""), "chars")
            check("the server introduces itself and says how to use it",
                  bool(getattr(init, "instructions", "")))

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print("tools:", names)
            check("all six tools are offered",
                  names == ["academic_calendar", "course_facts", "department_overview", "list_departments",
                            "search_courses", "search_sections"])
            check("every tool is described and marked read-only",
                  all(t.description and t.annotations and
                      (getattr(t.annotations, "read_only_hint", None) or getattr(t.annotations, "readOnlyHint", None))
                      for t in tools))

            async def call(name, **args):
                result = await session.call_tool(name, args)
                out = text_of(result)
                print(f"--- {name}({args}) -> {len(out)} chars: {out[:150]!r}")
                return out, result

            out, _ = await call("course_facts", course="CS 225")
            check("course_facts returns the course's title and prerequisites",
                  out.startswith("CS 225:") and "Prerequisites" in out)
            out, _ = await call("department_overview", subjects="BDI")
            check("department_overview lists courses with links", "| [BDI " in out and "?course=BDI-" in out)
            out, _ = await call("search_sections", subject="CS", course_number="225")
            check("search_sections returns sections with CRNs and meeting times", "CRN " in out and "Lecture" in out)
            out, _ = await call("search_sections", subject="STAT", level="4", starts_after="10:00", no_days="F", limit=5)
            check("search_sections applies schedule filters and the limit",
                  "section(s) in" in out and out.count("\nSTAT ") <= 5)
            out, _ = await call("search_sections", subject="BAD")
            check("an unknown department code gets the closest real one", "BADM" in out)
            out, _ = await call("search_courses", query="machine learning", subject="CS", limit=5)
            check("search_courses finds courses by title words", "Machine Learning" in out)
            out, _ = await call("academic_calendar", category="drop")
            check("academic_calendar returns dated events", "Academic calendar" in out and "20" in out)
            out, _ = await call("list_departments", query="gies")
            check("list_departments finds a college's departments", "BADM - Business Administration" in out)
            out, result = await call("search_sections")
            check("a call with nothing to search explains what to give",
                  "subject code" in out and not getattr(result, "is_error", getattr(result, "isError", False)))


asyncio.run(main())
print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
raise SystemExit(1 if failures else 0)
