"""
Checkpoint mechanism -- Phase B item 1 (reliability pass, 2026-09-13).

ROADMAP.md: "Turn Phase A's status-report fallback into a real checkpoint:
at a natural pause point, state exactly what can/can't be done and wait
for a decision, rather than guessing past it or dying quietly." Phase A
already gives a solid *resource-exhaustion* fallback (hit MAX_TURNS, get
an honest status report). What was still missing: a way for Alfred to
*voluntarily* pause mid-task at a genuine decision point -- not because it
ran out of turns, but because it hit a fork only a human can resolve.

Mechanism: a new `checkpoint` tool, no approval required (it's a
communication act, not an action). Calling it breaks the loop immediately
-- the same proven break/return pattern the approval gate already uses --
and the result carries a structured `awaiting_checkpoint` field
(done_summary/question/options) distinct from both a normal finished
reply and an approval request. Unlike the approval flow, resuming needs
no signature/replay machinery: it's not re-authorizing a dangerous call,
just continuing a conversation, so a plain "continue" turn with the
session's existing history is enough.

Same make_alfred() fake-router harness as the other Phase A/B test files.
No network, no real LLM keys. Run directly:

    python build-system/test_checkpoint.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor, TOOL_GUARDRAILS  # noqa: E402


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

async def _test_checkpoint_breaks_the_loop_with_a_structured_field():
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "12*7"}}',
        (
            '{"tool": "checkpoint", "params": {'
            '"done_summary": "Computed 12*7=84 for the first part.", '
            '"question": "Should I round the final total to 2 decimals or keep it exact?", '
            '"options": ["round to 2 decimals", "keep exact"]}}'
        ),
    ])
    result = await alfred.execute("do the multi-part calculation", {})
    await _drain_curation(alfred)

    assert result["awaiting_checkpoint"] is not None
    cp = result["awaiting_checkpoint"]
    assert cp["done_summary"] == "Computed 12*7=84 for the first part."
    assert cp["question"] == "Should I round the final total to 2 decimals or keep it exact?"
    assert cp["options"] == ["round to 2 decimals", "keep exact"]

    assert "Computed 12*7=84" in result["response"]
    assert "round the final total" in result["response"]
    assert result["awaiting_approval"] is None, "a checkpoint is not an approval request"
    assert result["tools_called"] == ["calculator", "checkpoint"]
    assert result["timings"]["turns_used"] == 2


async def _test_checkpoint_without_options_still_produces_a_clean_reply():
    alfred = make_alfred([
        '{"tool": "checkpoint", "params": {'
        '"done_summary": "Read all 3 files.", '
        '"question": "Want me to merge them into one summary or keep them separate?"}}'
    ])
    result = await alfred.execute("look at these three files", {})
    await _drain_curation(alfred)

    cp = result["awaiting_checkpoint"]
    assert cp["options"] is None
    assert "merge them into one summary" in result["response"]


async def _test_checkpoint_requires_a_question():
    """A checkpoint with no actual question isn't a checkpoint -- it's a
    malformed call, and should fail like any other missing-required-param
    tool call rather than silently pausing on nothing."""
    alfred = make_alfred([
        '{"tool": "checkpoint", "params": {"done_summary": "did stuff"}}',
        '{"reply": "Done anyway."}',
    ])
    result = await alfred.execute("do a thing", {})
    await _drain_curation(alfred)

    assert result["awaiting_checkpoint"] is None
    assert result["tool_results"][0]["success"] is False
    assert "question" in result["tool_results"][0]["output"].lower()


async def _test_checkpoint_does_not_require_approval():
    """A checkpoint is a communication act, not an action -- it must never
    itself be gated behind the approval flow it's meant to be a lighter
    alternative to."""
    guard = TOOL_GUARDRAILS.get("checkpoint")
    assert guard is None or not guard.require_approval


async def _test_normal_multi_turn_task_unaffected_by_checkpoint_addition():
    """Regression guard: a task that never calls checkpoint must behave
    exactly as it did before this feature existed."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "1+1"}}',
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "Done: 1+1=2, 2+2=4."}',
    ])
    result = await alfred.execute("do two sums", {})
    await _drain_curation(alfred)

    assert result["response"] == "Done: 1+1=2, 2+2=4."
    assert result["awaiting_checkpoint"] is None
    assert result["tools_called"] == ["calculator", "calculator"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_checkpoint_breaks_the_loop_with_a_structured_field():
    run(_test_checkpoint_breaks_the_loop_with_a_structured_field())


def test_checkpoint_without_options_still_produces_a_clean_reply():
    run(_test_checkpoint_without_options_still_produces_a_clean_reply())


def test_checkpoint_requires_a_question():
    run(_test_checkpoint_requires_a_question())


def test_checkpoint_does_not_require_approval():
    run(_test_checkpoint_does_not_require_approval())


def test_normal_multi_turn_task_unaffected_by_checkpoint_addition():
    run(_test_normal_multi_turn_task_unaffected_by_checkpoint_addition())


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
    print(f"\n{passed}/{len(tests)} checkpoint tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
