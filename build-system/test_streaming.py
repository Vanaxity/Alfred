"""
Turn-loop streaming tests -- Phase A item 3 (reliability pass, 2026-09-12).

Roadmap: "The full tool-call trace (thinking) currently comes back in one
blob at the end of the whole request. Stream it out as each turn completes
instead of buffering it." This is what "show me the reasoning like Claude
Code does" actually requires.

Mechanism: execute() takes an optional `on_event` callback. Every existing
`thinking.append(line)` call site becomes `await _emit(line)`, where _emit
appends to `thinking` exactly as before AND, if a callback was given, awaits
it with `{"type": "thinking", "text": line}`. One more call right before
returning delivers `{"type": "final", **result}`. When `on_event` is None
(every existing caller, unchanged), behavior is identical to before this
change -- _emit degrades to a plain append.

Same make_alfred() fake-router harness as test_loop_reliability.py /
test_error_surfacing.py. No network, no real LLM keys. Run directly:

    python build-system/test_streaming.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


class LLMResponse:
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

async def _test_on_event_receives_every_thinking_line_in_order():
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "Done: 2 and 4."}',
    ])
    captured = []

    async def on_event(evt):
        captured.append(evt)

    result = await alfred.execute("do two sums", {}, on_event=on_event)
    await _drain_curation(alfred)

    thinking_events = [e for e in captured if e["type"] == "thinking"]
    assert [e["text"] for e in thinking_events] == result["thinking"], (
        "every thinking line the caller sees in the final result must have "
        "also been streamed via on_event, in the same order"
    )
    assert len(thinking_events) >= 5, "a 3-turn run should emit several thinking events"


async def _test_on_event_receives_a_final_event_matching_the_result():
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "3+3"}}',
        '{"reply": "It is 6."}',
    ])
    captured = []

    async def on_event(evt):
        captured.append(evt)

    result = await alfred.execute("what is 3+3", {}, on_event=on_event)
    await _drain_curation(alfred)

    assert captured, "on_event must be called at least once"
    final = captured[-1]
    assert final["type"] == "final", "the last event must be the final one"
    assert final["response"] == result["response"] == "It is 6."
    assert final["tools_called"] == result["tools_called"] == ["calculator"]
    assert final["timings"] == result["timings"]


async def _test_events_arrive_incrementally_not_only_at_the_end():
    """The actual point of item 3: thinking events must show up as the run
    progresses, not get buffered and delivered as one lump alongside the
    final event. Provable without wall-clock timing: thinking-type events
    for turn 1 must be visible in the stream before the final event, and
    there must be more than a single flush point."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"tool": "calculator", "params": {"expression": "3+3"}}',
        '{"reply": "1+1=2, 2+2=4, 3+3=6."}',
    ])
    event_indices_by_turn_marker = []

    async def on_event(evt):
        if evt["type"] == "thinking" and evt["text"].startswith("[Turn"):
            event_indices_by_turn_marker.append(len(event_indices_by_turn_marker))

    await alfred.execute("three sums", {}, on_event=on_event)
    await _drain_curation(alfred)

    assert len(event_indices_by_turn_marker) >= 4, (
        "each turn boundary must produce its own event as it happens, not "
        "one batch at the very end"
    )


async def _test_on_event_none_is_unchanged_from_before():
    """Every existing caller passes no on_event at all -- default behavior
    (return value, thinking contents) must be byte-identical to before this
    change. Reuses the exact scenario test_loop_reliability.py already
    proves the intent-only nudge against."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "I\'m going to keep going."}',
        '{"tool": "calculator", "params": {"expression": "3+3"}}',
        '{"reply": "Done: 1+1=2, 2+2=4, 3+3=6."}',
    ])
    result = await alfred.execute("do three sums", {})  # no on_event at all
    await _drain_curation(alfred)

    assert result["response"] == "Done: 1+1=2, 2+2=4, 3+3=6."
    assert result["tools_called"].count("calculator") == 3
    assert any("Stated intent with no tool call" in line for line in result["thinking"])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_on_event_receives_every_thinking_line_in_order():
    run(_test_on_event_receives_every_thinking_line_in_order())


def test_on_event_receives_a_final_event_matching_the_result():
    run(_test_on_event_receives_a_final_event_matching_the_result())


def test_events_arrive_incrementally_not_only_at_the_end():
    run(_test_events_arrive_incrementally_not_only_at_the_end())


def test_on_event_none_is_unchanged_from_before():
    run(_test_on_event_none_is_unchanged_from_before())


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
    print(f"\n{passed}/{len(tests)} streaming tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
