"""
Error-surfacing tests -- Phase A item 4 (reliability pass, 2026-09-10).

Sam's complaint: "I don't get proper error messages. It stops after a
really long message." Reading brain/v2/conversation.py's execute() loop
confirmed three concrete holes where a real failure becomes either a
generic apology or an outright crash with the reasoning trace thrown away:

  1. The per-turn LLM router call is not wrapped. If all three providers
     fail, execute() raises straight out -- an unhandled traceback in the
     terminal, and via the API a bare "I encountered an error: <repr>"
     with the whole `thinking` list discarded.
  2. Any other mid-loop exception (prompt build, compression, the tool
     executor raising instead of returning success=False) does the same.
  3. The MAX_TURNS fallback only echoes the last *successful* tool result
     -- a run that ends on a failing tool call lists what it "did" and
     says nothing about the failure that actually blocked it.

These drive the real Alfred.execute() loop with a fake router (same
make_alfred() harness as test_loop_reliability.py / test_speed_audit_timing.py).
No network, no real LLM keys, no real vault. Run directly:

    python build-system/test_error_surfacing.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


class LLMResponse:
    """Shape-only stand-in for brain.llm_router.LLMResponse -- avoids
    importing llm_router.py, which pulls provider SDKs at module level.
    Mirrors the real class's fields, including `error`, which call() sets
    (with text=None) when every provider is down."""

    def __init__(self, text=None, provider=None, fallback_used=False,
                 fallback_reason=None, error=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason
        self.error = error


class FakeMemory:
    def get_context_for_llm(self, query=None):
        return ""

    def t3_find_episodes(self, query, max_results=2):
        return []

    def t3_save_episode(self, title, content):
        return "fake/path.md"


class FakeExpanded:
    def __init__(self, expanded):
        self.expanded = expanded


class FakeGoalExpander:
    async def expand(self, user_input):
        return FakeExpanded(user_input)


class FakeSkillManager:
    def find_skill(self, text, search_ecosystem=False):
        return None

    def generate_skill(self, **kwargs):
        return None

    def improve_skill(self, skill_id, note):
        return None


class FakeRouter:
    """Queued responses in order; repeats the last once exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        if self._responses:
            text = self._responses.pop(0)
        else:
            text = '{"reply": "nothing to save"}'
        return LLMResponse(text=text, provider="fake", fallback_used=False)


class RaisingRouter:
    """Every .call() raises -- an *unexpected* fault (a bug in message
    assembly, say), not the graceful provider-outage path below."""

    def __init__(self, exc):
        self._exc = exc
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        raise self._exc


class DeadRouter:
    """Mimics the real LLMRouter.call() when every provider is down: it
    returns an LLMResponse with text=None and .error set -- it does NOT
    raise. conversation.py used to read only .text, so this degraded to
    an empty reply every turn until MAX_TURNS."""

    def __init__(self):
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        return LLMResponse(
            text=None, provider=None, fallback_used=True,
            fallback_reason="terminal error: 401 invalid api key",
            error="All AI providers are currently unavailable. Please try again in a few minutes.",
        )


def make_alfred(router_responses, max_turns=None):
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory()
    a.skill_manager = FakeSkillManager()
    a.goal_expander = FakeGoalExpander()
    a.db = None
    a._router = FakeRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    if max_turns is not None:
        a.MAX_TURNS = max_turns
    return a


def run(coro):
    return asyncio.run(coro)


async def _drain_curation(alfred):
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_llm_provider_failure_is_surfaced_not_raised():
    """All providers down -> execute() must still return the normal dict,
    name the actual failure in the response, and keep the thinking trace.
    It must not let the exception escape."""
    alfred = make_alfred(["unused"])
    alfred._router = RaisingRouter(
        RuntimeError("all LLM providers failed: groq 503, gemini timeout, openai 429")
    )

    try:
        result = await alfred.execute("summarise my week", {})
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"execute() must catch the provider failure, not propagate it -- raised {e!r}"
        )
    await _drain_curation(alfred)

    assert isinstance(result, dict), "execute() must return its normal dict even on failure"
    assert result.get("response"), "there must be a user-facing message"
    assert "groq 503" in result["response"] or "all LLM providers failed" in result["response"], (
        f"the real error must be named, got: {result['response']!r}"
    )
    assert isinstance(result.get("thinking"), list) and result["thinking"], (
        "the reasoning trace must survive the error, not be discarded"
    )


async def _test_unexpected_midloop_exception_keeps_trace_and_prior_tool_calls():
    """A non-router exception mid-loop (here: prompt build throwing on the
    second turn, after a real tool call already happened) must be caught the
    same way -- normal dict, trace intact, and the work done before the
    blow-up still reported in tools_called."""
    alfred = make_alfred([
        '{"tool": "time", "params": {}}',
        '{"reply": "the time is above"}',
    ])

    real_build = alfred._build_system_prompt
    state = {"n": 0}

    def exploding_build(*args, **kwargs):
        state["n"] += 1
        if state["n"] >= 2:
            raise RuntimeError("prompt builder blew up on turn 2")
        return real_build(*args, **kwargs)

    alfred._build_system_prompt = exploding_build

    try:
        result = await alfred.execute("what time is it", {})
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"execute() must catch a mid-loop exception, not propagate it -- raised {e!r}"
        )
    await _drain_curation(alfred)

    assert isinstance(result, dict)
    assert "prompt builder blew up" in result["response"], (
        f"the real exception text must reach the user, got: {result['response']!r}"
    )
    assert result.get("tools_called") == ["time"], (
        "work completed before the failure must still be reported, "
        f"got tools_called={result.get('tools_called')!r}"
    )
    assert result["thinking"], "trace must survive"


async def _test_total_llm_outage_stops_fast_with_the_real_reason():
    """Every provider down -> the router returns text=None + .error (it
    does NOT raise). The loop must notice on turn 1 and say the LLM was
    unreachable -- not nudge an "empty reply" 30 times and then blame a
    step limit."""
    alfred = make_alfred(["unused"])
    alfred._router = DeadRouter()

    result = await alfred.execute("write up my week", {})
    await _drain_curation(alfred)

    assert result["timings"]["turns_used"] == 1, (
        "a dead LLM must stop on turn 1, not burn the whole budget -- "
        f"used {result['timings']['turns_used']}"
    )
    reply = result["response"].lower()
    assert "step limit" not in reply, (
        f"must not blame a step limit when the LLM was unreachable: {result['response']!r}"
    )
    assert ("language model" in reply or "provider" in reply
            or "invalid api key" in reply), (
        f"must name the real cause, got: {result['response']!r}"
    )
    assert alfred._router.call_count <= 2, (
        f"should not keep hammering a dead router -- called {alfred._router.call_count}x"
    )


async def _test_max_turns_fallback_reports_a_trailing_tool_failure():
    """When the loop exhausts its budget and the last thing that happened
    was a tool *failure*, the fallback must say so and include the real
    error text -- not just list the tool name as if it had worked."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "@@@ not math @@@"}}',
        '{"tool": "calculator", "params": {"expression": "### also not ###"}}',
    ], max_turns=2)

    result = await alfred.execute("work out that number for me", {})
    await _drain_curation(alfred)

    reply = result["response"]
    assert "step limit" in reply.lower(), "still the honest step-limit fallback"
    assert "calculator" in reply, "must name the tool it was using"
    assert "fail" in reply.lower(), (
        f"the fallback must admit the last step failed, got: {reply!r}"
    )
    assert "Calculator error" in reply, (
        f"the actual tool error text must be included, got: {reply!r}"
    )
    assert [tr["success"] for tr in result["tool_results"]] == [False, False]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_llm_provider_failure_is_surfaced_not_raised():
    run(_test_llm_provider_failure_is_surfaced_not_raised())


def test_unexpected_midloop_exception_keeps_trace_and_prior_tool_calls():
    run(_test_unexpected_midloop_exception_keeps_trace_and_prior_tool_calls())


def test_total_llm_outage_stops_fast_with_the_real_reason():
    run(_test_total_llm_outage_stops_fast_with_the_real_reason())


def test_max_turns_fallback_reports_a_trailing_tool_failure():
    run(_test_max_turns_fallback_reports_a_trailing_tool_failure())


def main():
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
            passed += 1
        except Exception:
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} error_surfacing tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
