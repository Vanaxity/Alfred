"""
Turn-loop reliability tests — 2026-09-09 reliability pass.

Two real bugs, both live-reproduced asking Alfred to "reverse-engineer
yourself": the loop stated intent ("I'm going to read the files") with
no tool call and stopped there as if that were a finished answer, and
separately MAX_TURNS=10 was nowhere near enough for genuinely large
tasks. These tests drive the full Alfred.execute() loop with a fake
router (same make_alfred() pattern as test_speed_audit_timing.py) rather
than just the pure phrase-detector logic (see test_context_manager.py
for those), since the actual bug lived in how the loop responds to a
scripted sequence of turns, not just whether one reply matches a phrase.

No network, no real LLM keys, no real vault. Run directly:
    python build-system/test_loop_reliability.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


class LLMResponse:
    """Local stand-in for brain.llm_router.LLMResponse's shape -- same
    rationale as test_speed_audit_timing.py's copy: avoids importing
    llm_router.py itself, which pulls in provider SDKs at module level."""

    def __init__(self, text=None, provider=None, fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


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
    """Returns queued responses in order; repeats the last one once
    exhausted (matches test_speed_audit_timing.py's FakeRouter)."""

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
    """Bare Alfred instance (Alfred.__new__, no __init__) with every heavy
    singleton swapped for a fake -- same construction as
    test_speed_audit_timing.py's make_alfred()."""
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

async def _test_intent_only_reply_after_real_tool_calls_gets_nudged_not_accepted():
    """The exact live-reproduced shape: real tool calls happen first
    (tools_called is non-empty), THEN a reply states intent with no tool
    call attached. Must be nudged into taking the next real step, not
    accepted as a finished answer -- confirming this fires regardless of
    prior tool history, unlike the completion-claim check it sits next to."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "I\'m going to keep going."}',            # the stall
        '{"tool": "calculator", "params": {"expression": "3+3"}}',
        '{"reply": "Done: 1+1=2, 2+2=4, 3+3=6."}',
    ])
    result = await alfred.execute("do three sums", {})
    await _drain_curation(alfred)

    assert result["response"] == "Done: 1+1=2, 2+2=4, 3+3=6.", (
        "the stall must not have become the final answer"
    )
    assert result["tools_called"].count("calculator") == 3, (
        "the loop must have continued past the stall to make the third real call"
    )
    assert any("Stated intent with no tool call" in line for line in result["thinking"]), (
        "the nudge must be visible in the thinking trace"
    )


async def _test_intent_only_reply_on_turn_one_also_gets_nudged():
    """The simpler case: no tools called yet at all when the stall
    happens. Must still be caught, same as the well-established
    completion-claim check already is for its own turn-1 case."""
    alfred = make_alfred([
        '{"reply": "I am going to check the weather now."}',   # the stall
        '{"tool": "weather", "params": {"location": "here"}}',
        '{"reply": "It is sunny."}',
    ])
    result = await alfred.execute("what's the weather", {})
    await _drain_curation(alfred)

    assert result["response"] == "It is sunny."
    assert result["tools_called"] == ["weather"]


async def _test_single_shot_nudge_does_not_loop_forever():
    """intent_only_nudge_used must be a one-shot flag -- if the model
    keeps stalling even after being told to act, the loop must not spin
    forever re-nudging the same thing turn after turn."""
    alfred = make_alfred([
        '{"reply": "I will now look into this."}',
        '{"reply": "I will now look into this."}',
        '{"reply": "I will now look into this."}',
    ], max_turns=5)
    result = await alfred.execute("investigate something", {})
    await _drain_curation(alfred)

    nudge_lines = [l for l in result["thinking"] if "Stated intent with no tool call" in l]
    assert len(nudge_lines) == 1, "the intent-only nudge must fire at most once per request"


async def _test_hitting_max_turns_produces_a_real_status_report():
    """When the loop genuinely exhausts its turn budget (not a nudge --
    real, repeated tool calls that never resolve to a final answer), the
    fallback must be an honest status report: what was actually done,
    the real turn-limit number, and an explicit invitation to continue --
    not a generic "I wasn't able to process that" shrug."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"tool": "calculator", "params": {"expression": "3+3"}}',
    ], max_turns=3)
    result = await alfred.execute("do a lot of math", {})
    await _drain_curation(alfred)

    reply = result["response"]
    assert "step limit" in reply.lower(), "must name the real constraint, not a vague apology"
    assert "3 steps" in reply or "(3 " in reply, "must state the actual turn limit, not a generic number"
    assert "calculator" in reply, "must actually say what tool(s) it used"
    assert "continue" in reply.lower(), "must explicitly invite continuing, not just apologize"
    assert result["tools_called"] == ["calculator", "calculator", "calculator"]


def test_max_turns_raised_from_ten():
    assert Alfred.MAX_TURNS == 30, "MAX_TURNS was raised from 10 -- confirm it wasn't silently reverted"


def test_intent_only_reply_after_real_tool_calls_gets_nudged_not_accepted():
    run(_test_intent_only_reply_after_real_tool_calls_gets_nudged_not_accepted())


def test_intent_only_reply_on_turn_one_also_gets_nudged():
    run(_test_intent_only_reply_on_turn_one_also_gets_nudged())


def test_single_shot_nudge_does_not_loop_forever():
    run(_test_single_shot_nudge_does_not_loop_forever())


def test_hitting_max_turns_produces_a_real_status_report():
    run(_test_hitting_max_turns_produces_a_real_status_report())


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
    print(f"\n{passed}/{len(tests)} loop_reliability tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
