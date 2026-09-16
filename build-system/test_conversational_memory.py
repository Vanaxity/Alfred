"""
Conversational-memory gap fix -- reliability pass, 2026-09-14.

Live-found testing with 20 fake life/advice scenarios (career, stress,
procrastination, sadness, happiness, etc.) plus an 8-turn deep conversation:
18/18 pure advice/emotional-support exchanges saved ZERO T3 episodes, and a
7-turn deep conversation about a real career decision only saved 1 turn
(the one that happened to state a standing fact and trigger `remember`) --
the other 6, the actual depth, left no trace. Root cause: episode-saving
required `any(t != "chat" for t in tools_called)` -- a pure-conversation
turn (the exact "talk to Alfred for life advice" use case) calls zero
tools, so the gate was never satisfied, ever, regardless of how meaningful
the exchange was.

Fix: save when EITHER a real tool was used (existing, unchanged behavior)
OR the task itself is substantive (more than a handful of words) -- catches
real conversational depth while still excluding pure filler ("thanks!",
"ok"). Not a perfect proxy for "meaningful," but the prior state ("almost
never") was far worse than this ("usually, unless clearly trivial").

Same make_alfred() fake-router harness as the other reliability-pass test
files. No network, no real LLM keys. Run directly:

    python build-system/test_conversational_memory.py
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


class RecordingMemory:
    def __init__(self):
        self.saved = []

    def get_context_for_llm(self, query=None):
        return ""

    def t3_find_episodes(self, query, max_results=2):
        return []

    def t3_save_episode(self, title, content):
        self.saved.append({"title": title, "content": content})
        return f"fake/{len(self.saved)}.md"


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
    a.memory = RecordingMemory()
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

async def _test_substantive_conversation_with_no_tools_now_saves():
    """The actual bug: a real, meaningful advice exchange with zero tool
    calls must now be saved -- this is the exact shape of all 18 failing
    live scenarios (career, stress, sadness, etc.)."""
    alfred = make_alfred([
        '{"reply": "That sounds like a lot to carry. Let'"'"'s break down what'"'"'s actually on your plate and figure out what can wait."}',
    ])
    task = (
        "I have so much on my plate right now between school and everything "
        "else, I genuinely can't think straight."
    )
    result = await alfred.execute(task, {})
    await _drain_curation(alfred)

    assert result["tools_called"] == [], "this scenario must not need any tool"
    assert result["episode_path"] is not None, (
        "a substantive tool-less conversation must now be saved"
    )
    assert len(alfred.memory.saved) == 1


async def _test_trivial_short_reply_still_does_not_save():
    """The flip side: pure filler must not flood the vault just because the
    tool-call requirement was relaxed."""
    for trivial in ["thanks!", "ok cool", "sounds good", "got it, thanks"]:
        alfred = make_alfred(['{"reply": "Anytime!"}'])
        result = await alfred.execute(trivial, {})
        await _drain_curation(alfred)
        assert result["episode_path"] is None, f"{trivial!r} must not save an episode"


async def _test_real_tool_use_still_saves_regardless_of_task_length():
    """Unchanged existing behavior: a short task that used a real tool
    still saves, exactly as before this fix."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "4."}',
    ])
    result = await alfred.execute("2+2?", {})
    await _drain_curation(alfred)

    assert result["tools_called"] == ["calculator"]
    assert result["episode_path"] is not None


async def _test_perishable_only_short_task_still_does_not_save():
    """Unchanged existing behavior: a short, purely factual time/weather
    ask -- no real conversational content -- still doesn't save, exactly
    as before (the original stale-clock-reuse guard)."""
    alfred = make_alfred([
        '{"tool": "time", "params": {}}',
        '{"reply": "It'"'"'s 3pm."}',
    ])
    result = await alfred.execute("what time is it", {})
    await _drain_curation(alfred)

    assert result["tools_called"] == ["time"]
    assert result["episode_path"] is None


async def _test_perishable_tool_alongside_substantive_content_does_save():
    """A time/weather check that's also carrying real substantive content
    should save for the content's sake -- the perishable exclusion was
    about not persisting a STALE READING as if current, not about
    silencing every message that happens to mention the time."""
    alfred = make_alfred([
        '{"tool": "time", "params": {}}',
        '{"reply": "It'"'"'s 3pm -- and for what it'"'"'s worth, feeling behind at 3pm doesn'"'"'t mean the day is lost."}',
    ])
    task = (
        "what time is it, I've been feeling really overwhelmed with "
        "everything today and it's stressing me out"
    )
    result = await alfred.execute(task, {})
    await _drain_curation(alfred)

    assert result["episode_path"] is not None


async def _test_self_summary_query_still_never_saves_even_if_substantive():
    """Item 5's rule must survive this change: a long, wordy 'what do you
    know about me' style question still must never save, regardless of
    length."""
    alfred = make_alfred(['{"reply": "Here is everything I have on file about you..."}'])
    task = "Could you please tell me absolutely everything you know about me so far?"
    result = await alfred.execute(task, {})
    await _drain_curation(alfred)

    assert result["episode_path"] is None


async def _test_prompt_forbids_embellishing_across_retrieved_episodes():
    """The confabulation found live: recalling a real saved episode plus
    inventing an unstated detail ("core values: meaningful work") on top
    of it. The prompt must explicitly forbid this."""
    alfred = make_alfred(['{"reply": "x"}'])
    system, _dropped = alfred._build_system_prompt(memory_snippets=["some past episode"], matched_skill=None)
    low = system.lower()
    assert "only state what's actually" in low or "never add a detail" in low or "don't add plausible" in low, (
        "the prompt must explicitly forbid inventing connective details when "
        "synthesizing retrieved memory, not just when a lookup is fully empty"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_substantive_conversation_with_no_tools_now_saves():
    run(_test_substantive_conversation_with_no_tools_now_saves())


def test_trivial_short_reply_still_does_not_save():
    run(_test_trivial_short_reply_still_does_not_save())


def test_real_tool_use_still_saves_regardless_of_task_length():
    run(_test_real_tool_use_still_saves_regardless_of_task_length())


def test_perishable_only_short_task_still_does_not_save():
    run(_test_perishable_only_short_task_still_does_not_save())


def test_perishable_tool_alongside_substantive_content_does_save():
    run(_test_perishable_tool_alongside_substantive_content_does_save())


def test_self_summary_query_still_never_saves_even_if_substantive():
    run(_test_self_summary_query_still_never_saves_even_if_substantive())


def test_prompt_forbids_embellishing_across_retrieved_episodes():
    run(_test_prompt_forbids_embellishing_across_retrieved_episodes())


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
    print(f"\n{passed}/{len(tests)} conversational_memory tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
