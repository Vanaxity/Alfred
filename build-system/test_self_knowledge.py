"""
Self-knowledge tests -- Phase A item 5 (reliability pass, 2026-09-10).

Sam: "I asked it to tell me about myself, and it hallucinated fake facts."

Root cause (traced live against the real vault, not guessed):
  - A prompt rule forced `memory_search` for "what do you know about X"
    phrasings AND told the model not to "just recite the prompt" -- so a
    broad self-summary question could no longer be answered from the T4
    profile that was sitting right there in the system prompt.
  - `t4_search("what do you know about me")` returns nothing (it is a
    keyword/exact-key matcher; a natural-language query matches none of
    the ~40 stored keys).
  - So the model answered a rich-profile question from a near-empty
    lookup, and filled the gap by inventing.
  - Every such turn also saved its own answer as a T3 episode, so a weak
    or invented answer resurfaced as "memory" the next time.

This pass fixes it at the cheap end only (Sam's call: A + D + E):
  A. reword the rule so broad self-summary questions are answered from
     the profile, not a forced lookup;
  D. add an explicit "never invent facts about Master Sam" rule;
  E. stop saving a T3 episode for self-summary turns.

Drives the real loop / prompt builder with fakes -- no network, no keys,
no real vault. Run directly:

    python build-system/test_self_knowledge.py
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
    """FakeMemory that records every t3_save_episode call so a test can
    assert one did / did not happen."""

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


def make_alfred(router_responses):
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
# E -- T3 episode not saved for self-summary turns
# ---------------------------------------------------------------------------

async def _test_self_summary_turn_saves_no_t3_episode():
    """"Tell me about myself" + a memory_search call must NOT persist a T3
    episode -- otherwise the answer (weak or invented) resurfaces as
    'memory' next time the same thing is asked."""
    alfred = make_alfred([
        '{"tool": "memory_search", "params": {"query": "about me", "tier": "all"}}',
        '{"reply": "You are Sam, 15, from Solapur. Founder of Alfred."}',
    ])
    result = await alfred.execute("Tell me about myself.", {})
    await _drain_curation(alfred)

    assert result["episodes_saved"] == 0, "a self-summary turn must not save an episode"
    assert result["episode_path"] is None
    assert alfred.memory.saved == [], (
        f"t3_save_episode was called for a self-summary turn: {alfred.memory.saved}"
    )


async def _test_self_summary_detection_matches_the_real_phrasings():
    check = Alfred.__dict__["_is_self_summary_query"].__func__
    for yes in [
        "Tell me about myself.", "tell me about me",
        "What do you know about me?", "who am I",
        "what do you have on me", "describe me",
        "everything you know about me",
    ]:
        assert check(yes) is True, f"should be a self-summary query: {yes!r}"
    for no in [
        "what's 2 + 2", "what do you know about my chemistry teacher",
        "remind me about the meeting", "tell me about the weather",
        "search your memory for the TKS thing",
    ]:
        assert check(no) is False, f"should NOT be a self-summary query: {no!r}"


async def _test_a_normal_tool_turn_still_saves_its_episode():
    """Guard against E over-reaching: an ordinary task that used a real
    (non-perishable) tool must still be persisted."""
    alfred = make_alfred([
        '{"tool": "calculator", "params": {"expression": "2+2"}}',
        '{"reply": "That is 4."}',
    ])
    result = await alfred.execute("what is two plus two", {})
    await _drain_curation(alfred)

    assert result["episodes_saved"] == 1, "a normal calculator turn should still save an episode"
    assert len(alfred.memory.saved) == 1


# ---------------------------------------------------------------------------
# A + D -- the system prompt itself
# ---------------------------------------------------------------------------

def _system_prompt():
    alfred = make_alfred(['{"reply": "x"}'])
    system, _dropped = alfred._build_system_prompt(memory_snippets=[], matched_skill=None)
    return system


async def _test_prompt_answers_broad_self_questions_from_the_profile():
    """A: the rule must send broad self-summary questions to the Profile,
    and must NOT still carry the old 'rather than just reciting the
    prompt' wording that severed it."""
    system = _system_prompt().lower()
    assert "rather than just reciting the prompt" not in system, (
        "the old rule wording that blocked profile-based self-summary is still there"
    )
    assert "tell me about myself" in system or "tell me about yourself" in system, (
        "the rule should name the broad self-summary case explicitly"
    )
    assert "profile" in system


async def _test_prompt_forbids_inventing_facts_about_the_user():
    """D: an explicit anti-invention rule must be present."""
    system = _system_prompt().lower()
    assert "never invent facts about master sam" in system, (
        "the explicit anti-invention rule (D) is missing from the prompt"
    )
    assert "don't have it saved" in system or "do not have it saved" in system


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_self_summary_turn_saves_no_t3_episode():
    run(_test_self_summary_turn_saves_no_t3_episode())


def test_self_summary_detection_matches_the_real_phrasings():
    run(_test_self_summary_detection_matches_the_real_phrasings())


def test_a_normal_tool_turn_still_saves_its_episode():
    run(_test_a_normal_tool_turn_still_saves_its_episode())


def test_prompt_answers_broad_self_questions_from_the_profile():
    run(_test_prompt_answers_broad_self_questions_from_the_profile())


def test_prompt_forbids_inventing_facts_about_the_user():
    run(_test_prompt_forbids_inventing_facts_about_the_user())


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
    print(f"\n{passed}/{len(tests)} self_knowledge tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
