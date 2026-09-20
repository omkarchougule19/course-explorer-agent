"""
test_llm_failover.py

Offline check (no network, no key spent) that the Groq client falls back to a
second model on a rate-limit error, and only on a rate-limit error:

    .venv/Scripts/python -m evals.test_llm_failover
"""

import os

import groq
import httpx
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables.fallbacks import RunnableWithFallbacks

from app import agent

failures = []


def check(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def _rate_limit_error():
    req = httpx.Request("POST", "https://api.groq.com/x")
    return groq.RateLimitError("429", response=httpx.Response(429, request=req), body=None)


class _Raises(FakeListChatModel):
    exc: Exception = None

    def _call(self, *args, **kwargs):
        raise self.exc


def _env(**kw):
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER",
              "GROQ_MODEL", "GROQ_FALLBACK_MODEL"):
        os.environ.pop(k, None)
    os.environ.update(kw)


# 1. wiring: a fallback wrapper by default, plain client when disabled or same model
_env(GROQ_API_KEY="k")
llm, _ = agent._build_llm()
check("default: primary wrapped with a fallback", isinstance(llm, RunnableWithFallbacks))
check("default: fallback is qwen", llm.fallbacks[0].model_name == agent.DEFAULT_GROQ_FALLBACK)
check("default: primary is gpt-oss-120b", llm.runnable.model_name == "openai/gpt-oss-120b")

_env(GROQ_API_KEY="k", GROQ_FALLBACK_MODEL="off")
llm, _ = agent._build_llm()
check("GROQ_FALLBACK_MODEL=off: no wrapper", not isinstance(llm, RunnableWithFallbacks))

_env(GROQ_API_KEY="k", GROQ_MODEL="qwen/qwen3.8-27b")
llm, _ = agent._build_llm()
check("primary already equals the fallback: no wrapper", not isinstance(llm, RunnableWithFallbacks))

# 2. behavior: 429 on the primary is answered by the fallback; other errors are not swallowed
good = FakeListChatModel(responses=["from fallback"])
rl = _Raises(responses=["x"], exc=_rate_limit_error())
wrapped = rl.with_fallbacks([good], exceptions_to_handle=(groq.RateLimitError,))
check("429 on primary -> fallback answers", wrapped.invoke("hi").content == "from fallback")

boom = _Raises(responses=["x"], exc=ValueError("not a rate limit"))
wrapped = boom.with_fallbacks([good], exceptions_to_handle=(groq.RateLimitError,))
try:
    wrapped.invoke("hi")
    check("non-429 error still raises", False)
except ValueError:
    check("non-429 error still raises", True)

# 3. the agent still builds around the wrapper (tool-calling needs bind_tools)
_env(GROQ_API_KEY="k")
try:
    agent.build_agent()
    check("build_agent works with the fallback wrapper", True)
except Exception as exc:  # noqa: BLE001
    print("       ", repr(exc)[:200])
    check("build_agent works with the fallback wrapper", False)


# 4. ask() / astream_answer() retry once on the fallback model after a Groq 429
import asyncio

_calls = []


class _FakeAgent:
    def __init__(self, model, fail):
        self.model, self.fail = model, fail

    def invoke(self, *a, **k):
        _calls.append(("invoke", self.model))
        if self.fail:
            raise _rate_limit_error()
        return {"output": f"answer from {self.model or 'primary'}"}

    async def astream_events(self, *a, **k):
        _calls.append(("stream", self.model))
        if self.fail:
            raise _rate_limit_error()
        yield {"event": "on_chain_end", "name": "AgentExecutor",
               "data": {"output": {"output": f"answer from {self.model or 'primary'}"}}}


_real_build, _real_empty = agent.build_agent, agent._sections_empty
agent._sections_empty = lambda: False


def _fake_build(fail_primary=True, fail_fallback=False):
    def build(verbose=False, streaming=False, model=None):
        return _FakeAgent(model, fail_fallback if model else fail_primary)
    return build


_env(GROQ_API_KEY="k")
FB = agent.DEFAULT_GROQ_FALLBACK

agent.build_agent = _fake_build()
_calls.clear()
out = agent.ask("who teaches CS 225")
check("ask(): 429 on primary retried on fallback", f"answer from {FB}" in out)
check("ask(): primary tried first, fallback second",
      _calls == [("invoke", None), ("invoke", FB)])

agent.build_agent = _fake_build(fail_fallback=True)
_calls.clear()
out = agent.ask("who teaches CS 225")
check("ask(): fallback also 429 -> friendly error, no third try", len(_calls) == 2 and "answer from" not in out)

_env(GROQ_API_KEY="k", GROQ_FALLBACK_MODEL="off")
agent.build_agent = _fake_build()
_calls.clear()
agent.ask("who teaches CS 225")
check("ask(): fallback off -> no retry", len(_calls) == 1)

_env(GROQ_API_KEY="k")


async def _drain(gen):
    return [item async for item in gen]


agent.build_agent = _fake_build()
_calls.clear()
items = asyncio.run(_drain(agent.astream_answer("who teaches CS 225")))
done = [t for k, t in items if k == "done"]
check("stream: 429 before any text retried on fallback", bool(done) and f"answer from {FB}" in done[-1])
check("stream: emits a 'switching' status", any(k == "status" and "backup" in t for k, t in items))
check("stream: exactly one done event", len(done) == 1)

agent.build_agent, agent._sections_empty = _real_build, _real_empty

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all checks passed")
