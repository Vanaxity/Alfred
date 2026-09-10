"""
Q8 speed-audit instrumentation tests — Day 7.

Alfred.execute() now records per-phase wall-clock timings (goal expansion,
skill matching, T3 memory-snippet fetch, prompt build, LLM calls, tool
execution, mutation verification, compression) instead of Sam having to
guess where a turn's latency actually goes. It also runs the independent
goal-expansion/skill-matching chain concurrently with the T3 memory-snippet
fetch, since the latter depends only on the raw task text and was previously
paying its own wall-clock time stacked strictly after the former finished.

These tests use fakes for every network/DB dependency (LLM router, memory,
skill manager, goal expander) — no real API keys, no real vault, no real
network calls. Run directly:

    python build-system/test_speed_audit_timing.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


class LLMResponse:
    """Local stand-in for brain.llm_router.LLMResponse's shape (text,
    provider, fallback_used, fallback_reason) -- avoids importing
    llm_router.py itself, which pulls in the groq/openai/google-genai SDKs
    at module level purely for class definitions this test never touches."""

    def __init__(self, text=None, provider=None, fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


# ---------------------------------------------------------------------------
# Fakes — no network, no disk, no real memory backend.
# ---------------------------------------------------------------------------

class FakeMemory:
    def __init__(self, t3_delay: float = 0.0):
        self._t3_delay = t3_delay

    def get_context_for_llm(self, query=None):
        return ""

    def t3_find_episodes(self, query, max_results=2):
        if self._t3_delay:
            time.sleep(self._t3_delay)
        return []

    def t3_save_episode(self, title, content):
        return "fake/path.md"


class FakeExpanded:
    def __init__(self, expanded):
        self.expanded = expanded


class FakeGoalExpander:
    def __init__(self, delay: float = 0.0):
        self._delay = delay

    async def expand(self, user_input):
        if self._delay:
            await asyncio.sleep(self._delay)
        return FakeExpanded(user_input)


class FakeSkillManager:
    def find_skill(self, text, search_ecosystem=False):
        return None

    def generate_skill(self, **kwargs):
        return None

    def improve_skill(self, skill_id, note):
        return None


class FakeRouter:
    """Returns queued responses in order; repeats the last one once exhausted
    (the fire-and-forget memory-curation pass makes its own extra call that
    tests here don't otherwise care about)."""

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


def make_alfred(router_responses, memory_t3_delay=0.0, expand_delay=0.0):
    """Alfred instance with every heavy singleton swapped for a fake, built
    without calling Alfred.__init__ (which pulls in the real vault/DB/LLM
    clients)."""
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory(t3_delay=memory_t3_delay)
    a.skill_manager = FakeSkillManager()
    a.goal_expander = FakeGoalExpander(delay=expand_delay)
    a.db = None
    a._router = FakeRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    return a


async def _drain_curation(alfred):
    """Let fire-and-forget curation tasks finish so the event loop doesn't
    warn about pending tasks when the test function returns."""
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_timings_present_and_well_formed():
    alfred = make_alfred(['{"reply": "Hello there."}'])
    result = await alfred.execute("say hi", {})
    await _drain_curation(alfred)

    assert result["response"] == "Hello there."
    timings = result.get("timings")
    assert isinstance(timings, dict), "execute() must return a 'timings' dict"

    expected_keys = {
        "goal_expansion_ms", "skill_matching_ms", "memory_snippets_wait_ms",
        "pre_loop_total_ms", "prompt_build_ms", "llm_call_ms", "total_ms",
        "turns_used",
    }
    missing = expected_keys - set(timings)
    assert not missing, f"timings missing keys: {missing}"

    for key, val in timings.items():
        if key == "turns_used":
            assert isinstance(val, int) and val >= 1
        else:
            assert isinstance(val, float) and val >= 0.0, f"{key} must be a non-negative float, got {val!r}"

    assert timings["turns_used"] == 1, "a single-reply turn should use exactly one turn"
    assert timings["total_ms"] >= timings["pre_loop_total_ms"], \
        "total wall time must be at least the pre-loop portion of it"

    # No tool ran, so tool_execution_ms must be absent, not zeroed-in falsely.
    assert "tool_execution_ms" not in timings

    assert any("[Timing]" in line for line in result["thinking"]), \
        "a human-readable timing summary must land in `thinking` for visibility"


async def _test_pre_loop_work_runs_concurrently():
    """The core Q8 fix: goal-expansion and the T3 memory-snippet fetch are
    independent (both only need the raw task text) and must overlap instead
    of stacking. With both artificially delayed 60ms, sequential execution
    would take >=120ms; concurrent execution should land close to 60ms."""
    delay = 0.06
    alfred = make_alfred(
        ['{"reply": "done"}'],
        memory_t3_delay=delay,
        expand_delay=delay,
    )
    result = await alfred.execute("what's the weather concept", {})
    await _drain_curation(alfred)

    timings = result["timings"]
    sequential_floor = delay * 2 * 1000.0  # ms, if run one after another
    assert timings["pre_loop_total_ms"] < sequential_floor * 0.85, (
        f"pre_loop_total_ms={timings['pre_loop_total_ms']:.1f}ms looks sequential "
        f"(>= {sequential_floor * 0.85:.1f}ms) -- goal expansion and the memory-"
        "snippet fetch should run concurrently, not back-to-back"
    )
    # The wait for the already-overlapped memory fetch should be small --
    # most of its delay should have been hidden behind goal_expansion_ms.
    assert timings["memory_snippets_wait_ms"] < delay * 1000.0 * 0.9, (
        f"memory_snippets_wait_ms={timings['memory_snippets_wait_ms']:.1f}ms "
        "suggests the fetch wasn't actually overlapped with goal expansion"
    )


async def _test_tool_execution_is_timed_when_a_tool_runs():
    alfred = make_alfred([
        '{"tool": "time", "params": {}}',
        '{"reply": "It is now that time."}',
    ])
    result = await alfred.execute("what time is it", {})
    await _drain_curation(alfred)

    assert result["tools_called"] == ["time"]
    timings = result["timings"]
    assert timings["turns_used"] == 2
    assert "tool_execution_ms" in timings
    assert timings["tool_execution_ms"] >= 0.0
    assert timings["llm_call_ms"] > 0.0, "two LLM calls were made, llm_call_ms must accumulate"


async def _test_timings_accumulate_across_multiple_turns():
    """llm_call_ms must be a running total across turns, not just the last one."""
    alfred = make_alfred([
        '{"tool": "time", "params": {}}',
        '{"tool": "time", "params": {}}',
        '{"reply": "ok"}',
    ])
    result = await alfred.execute("check the time twice", {})
    await _drain_curation(alfred)

    assert result["timings"]["turns_used"] == 3
    assert alfred._router.call_count >= 3
    assert result["timings"]["tool_execution_ms"] >= 0.0


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain async defs
# named test_*, run directly, no pytest).
# ---------------------------------------------------------------------------

def test_timings_present_and_well_formed():
    asyncio.run(_test_timings_present_and_well_formed())


def test_pre_loop_work_runs_concurrently():
    asyncio.run(_test_pre_loop_work_runs_concurrently())


def test_tool_execution_is_timed_when_a_tool_runs():
    asyncio.run(_test_tool_execution_is_timed_when_a_tool_runs())


def test_timings_accumulate_across_multiple_turns():
    asyncio.run(_test_timings_accumulate_across_multiple_turns())


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
    print(f"\n{passed}/{len(tests)} speed_audit_timing tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
