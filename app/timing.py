"""Per-request timing for /ask and /ask/stream.

Each question gets a Timeline: a list of steps (name, when it started relative
to the request, how long it took, plus a few details such as the model or the
tool). The route starts it, the agent and its tools add to it, and the route
writes it to ask_log.timings (JSON) and prints one `[timing]` line to the
server log when the answer is finished. The point is to see where a slow
answer spends its time in production - the LLM calls, the tools, the
database, a provider's rate-limit wait, or a cold process - instead of only
the total that ask_log.latency_ms has always held.

The current Timeline lives in a context variable, so code deep inside a tool
can add a step without being handed it; LangChain copies the context into the
threads it runs sync tools in, so those steps land in the right request.
Nothing here may raise into the request: every recording path is best effort.
"""

import contextvars
import json
import threading
import time
from contextlib import contextmanager

_PROCESS_START = time.monotonic()
_FIRST = {"seen": False}
_FIRST_LOCK = threading.Lock()


class Timeline:
    def __init__(self, route: str):
        self.t0 = time.monotonic()
        self.steps: list[dict] = []
        self.meta: dict = {"route": route}
        with _FIRST_LOCK:
            self.meta["first_request"] = not _FIRST["seen"]
            _FIRST["seen"] = True
        self.meta["uptime_s"] = int(self.t0 - _PROCESS_START)

    def add(self, step: str, start: float, end: "float | None" = None, **extra) -> None:
        end = time.monotonic() if end is None else end
        self.steps.append({"step": step, "at_ms": int((start - self.t0) * 1000),
                           "ms": int((end - start) * 1000),
                           **{k: v for k, v in extra.items() if v is not None}})

    def mark(self, key: str) -> None:
        """Record when something happened (e.g. first_token_ms), once."""
        self.meta.setdefault(key, int((time.monotonic() - self.t0) * 1000))

    def total_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def to_json(self) -> str:
        return json.dumps({**self.meta, "total_ms": self.total_ms(), "steps": self.steps},
                          separators=(",", ":"), default=str)

    def summary(self) -> str:
        """One log line: the total, then each step's duration in order."""
        parts = [f"total={self.total_ms()}ms"]
        parts += [f"{k}={v}" for k, v in self.meta.items() if k != "route"]
        for s in self.steps:
            label = s["step"] + (f"[{s['name']}]" if s.get("name") else "")
            parts.append(f"{label}=wait{s['secs']}s" if "secs" in s else f"{label}={s['ms']}ms")
        return f"[timing] {self.meta['route']} " + " ".join(parts)


_CURRENT: contextvars.ContextVar = contextvars.ContextVar("ask_timeline", default=None)


def start(route: str) -> Timeline:
    tl = Timeline(route)
    _CURRENT.set(tl)
    return tl


def activate(tl: "Timeline | None") -> None:
    """Make `tl` current in this context (a streaming generator runs in a
    different context from the route function that started it)."""
    _CURRENT.set(tl)


def current() -> "Timeline | None":
    return _CURRENT.get()


@contextmanager
def span(step: str, **extra):
    """Time the block as one step of the current Timeline (no-op without one)."""
    tl = _CURRENT.get()
    t = time.monotonic()
    try:
        yield
    finally:
        if tl is not None:
            try:
                tl.add(step, t, **extra)
            except Exception:  # noqa: BLE001 - timing must never break a request
                pass


def note(step: str, **extra) -> None:
    """A zero-length step: something that happened, with details."""
    tl = _CURRENT.get()
    if tl is not None:
        t = time.monotonic()
        tl.add(step, t, t, **extra)


_CALLBACK_CLS = None


def callback(tl: "Timeline | None"):
    """A LangChain callback handler that adds every LLM call and tool call of
    one agent run to `tl`: the model, the token counts, how long the model
    took to send its first token (streaming), and which tool an LLM call was
    made inside (RAG query expansion). Defined lazily so importing this module
    doesn't pull in LangChain."""
    global _CALLBACK_CLS
    if _CALLBACK_CLS is None:
        from langchain_core.callbacks import BaseCallbackHandler

        class _TimingCallback(BaseCallbackHandler):
            run_inline = True

            def __init__(self, tl):
                self.tl = tl
                self.open: dict = {}   # run_id -> (start, kind, name, parent tool name)
                self.first_token: dict = {}

            def _begin(self, run_id, kind, name, parent_run_id):
                parent = self.open.get(parent_run_id)
                inside = parent[2] if parent and parent[1] == "tool" else None
                self.open[run_id] = (time.monotonic(), kind, name, inside)

            def _finish(self, run_id, error=None, **extra):
                began = self.open.pop(run_id, None)
                if not began or self.tl is None:
                    return
                t, kind, name, inside = began
                ttft = self.first_token.pop(run_id, None)
                self.tl.add(kind, t, name=name, inside=inside,
                            first_token_ms=int((ttft - t) * 1000) if ttft else None,
                            error=(repr(error)[:200] if error else None), **extra)

            def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
                params = kw.get("invocation_params") or {}
                name = params.get("model") or params.get("model_name") or (serialized or {}).get("name")
                self._begin(run_id, "llm", name, parent_run_id)

            def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw):
                self.on_chat_model_start(serialized, prompts, run_id=run_id,
                                         parent_run_id=parent_run_id, **kw)

            def on_llm_new_token(self, token, *, run_id, **kw):
                self.first_token.setdefault(run_id, time.monotonic())

            def on_llm_end(self, response, *, run_id, **kw):
                tokens_in = tokens_out = None
                try:
                    msg = response.generations[0][0].message
                    usage = getattr(msg, "usage_metadata", None) or {}
                    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
                except Exception:  # noqa: BLE001 - not every provider reports usage
                    pass
                if tokens_in is None:
                    usage = ((response.llm_output or {}).get("token_usage") or {})
                    tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
                self._finish(run_id, tokens_in=tokens_in, tokens_out=tokens_out)

            def on_llm_error(self, error, *, run_id, **kw):
                self._finish(run_id, error=error)

            def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw):
                self._begin(run_id, "tool", (serialized or {}).get("name") or kw.get("name"),
                            parent_run_id)

            def on_tool_end(self, output, *, run_id, **kw):
                self._finish(run_id, out_chars=len(str(output or "")))

            def on_tool_error(self, error, *, run_id, **kw):
                self._finish(run_id, error=error)

        _CALLBACK_CLS = _TimingCallback
    return _CALLBACK_CLS(tl)
