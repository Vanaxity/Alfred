"""
Approval-signature flake fix -- Phase B item 2 (reliability pass, 2026-09-12).

Root cause, traced by a dedicated investigation (not guessed): the approval
round-trip spans two separate execute() calls with two separate
ConversationHistory objects. process_chat() only ever persists the canned
human-readable reply ("I need your approval before running X...") to
session history -- never the actual tool params. So on a resend, the second
execute() call has no record of what was originally proposed; the LLM
isn't "retrying" a known call, it's regenerating a brand-new one from vague
text that doesn't even name the params. There is nothing consistent for it
to reproduce, so the signature it emits essentially never matches. No
client could ever have satisfied the old bare-signature contract.

Fix: the client now echoes back the *whole* awaiting_approval object(s)
({"tool", "params", "signature"}) instead of a bare signature string.
execute() verifies each signature against its own params (never trusts a
client-supplied signature blindly) and, for anything that checks out,
executes that exact call directly -- before the turn loop even starts, no
LLM involved in reproducing it.

Uses the real ToolExecutor (create_tool_executor()) plus one extra
approval-gated fake tool registered just for these tests, so the actual
guardrail/_action_signature machinery is exercised for real -- only the
tool's own side effect is faked. No network, no real LLM keys. Run
directly:

    python build-system/test_approval_signature.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import (  # noqa: E402
    Guardrails,
    ToolResult,
    _action_signature,
    create_tool_executor,
)


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


_SIDE_EFFECT_LOG = []


async def _fake_gated_handler(params, ctx):
    """Stands in for shell/run_code/etc: the thing being approval-gated.
    No real side effect -- just records that it actually ran, with what."""
    _SIDE_EFFECT_LOG.append(dict(params))
    return ToolResult(success=True, output=f"did the thing with {params}")


def make_alfred(router_responses, max_turns=None):
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory()
    a.skill_manager = FakeSkillManager()
    a.goal_expander = FakeGoalExpander()
    a.db = None
    a._router = FakeRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._tool_executor.register(
        "test_needs_approval", _fake_gated_handler,
        guardrails=Guardrails(require_approval=True),
    )
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

async def _test_preapproved_action_executes_without_the_llm_reemitting_it():
    """The core fix: given a correctly-signed approved action up front, the
    tool actually runs BEFORE the loop even asks the LLM anything -- the
    LLM's only job this turn is to produce the final reply, not to
    reconstruct the call."""
    _SIDE_EFFECT_LOG.clear()
    alfred = make_alfred(['{"reply": "All done, thanks!"}'])
    sig = _action_signature("test_needs_approval", {"x": 1})
    context = {
        "approved_actions": [
            {"tool": "test_needs_approval", "params": {"x": 1}, "signature": sig}
        ]
    }

    result = await alfred.execute("please do the thing", context)
    await _drain_curation(alfred)

    assert _SIDE_EFFECT_LOG == [{"x": 1}], "the gated tool must actually have run"
    assert result["tools_called"] == ["test_needs_approval"]
    assert result["tool_results"][0]["success"] is True
    assert result["response"] == "All done, thanks!", (
        "only one router response was queued (the reply); if the loop had "
        "needed the LLM to emit the tool call too, this turn would have "
        "consumed that response as a wasted extra turn and the fallback "
        f"reply would show up instead -- got {result['response']!r}"
    )


async def _test_a_tampered_signature_is_rejected_not_executed():
    """A signature that doesn't match its own params -- forged, corrupted,
    or stale -- must never be trusted into running something."""
    _SIDE_EFFECT_LOG.clear()
    alfred = make_alfred(['{"reply": "ok"}'])
    context = {
        "approved_actions": [
            {"tool": "test_needs_approval", "params": {"x": 1}, "signature": "not-the-real-signature"}
        ]
    }

    result = await alfred.execute("please do the thing", context)
    await _drain_curation(alfred)

    assert _SIDE_EFFECT_LOG == [], "a mismatched signature must never cause execution"
    assert result["tools_called"] == []


async def _test_end_to_end_approval_round_trip_matches_the_real_bug_shape():
    """The actual bug, reproduced and proven fixed: turn 1 gets blocked
    pending approval; a fresh execute() call (a real client resend is a
    brand-new request/response, so a brand-new Alfred.execute() call is the
    faithful simulation) using nothing but the awaiting_approval object
    turn 1 returned must succeed -- with the LLM never being asked to
    reproduce the call."""
    _SIDE_EFFECT_LOG.clear()
    alfred = make_alfred([
        '{"tool": "test_needs_approval", "params": {"x": 42}}',
    ])
    result1 = await alfred.execute("do the gated thing", {})
    await _drain_curation(alfred)

    assert _SIDE_EFFECT_LOG == [], "must not have run yet -- approval pending"
    assert result1["awaiting_approval"] is not None
    approval = result1["awaiting_approval"]
    assert approval["tool"] == "test_needs_approval"
    assert approval["params"] == {"x": 42}
    assert approval["signature"] == _action_signature("test_needs_approval", {"x": 42})

    # Simulate the client's resend: a brand-new request, a brand-new Alfred
    # instance even (nothing server-side persisted the original call --
    # that's the whole point), echoing back exactly what turn 1 returned.
    alfred2 = make_alfred(['{"reply": "Done -- ran it."}'])
    result2 = await alfred2.execute(
        "yes, go ahead", {"approved_actions": [approval]}
    )
    await _drain_curation(alfred2)

    assert _SIDE_EFFECT_LOG == [{"x": 42}], "the originally-proposed call must run, unmodified"
    assert result2["tools_called"] == ["test_needs_approval"]
    assert result2["response"] == "Done -- ran it.", (
        "only one router response was queued for the visible turn; a leaked "
        "fallback reply here would mean the loop burned an extra turn "
        f"waiting on the LLM to re-emit the call -- got {result2['response']!r}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_preapproved_action_executes_without_the_llm_reemitting_it():
    run(_test_preapproved_action_executes_without_the_llm_reemitting_it())


def test_a_tampered_signature_is_rejected_not_executed():
    run(_test_a_tampered_signature_is_rejected_not_executed())


def test_end_to_end_approval_round_trip_matches_the_real_bug_shape():
    run(_test_end_to_end_approval_round_trip_matches_the_real_bug_shape())


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
    print(f"\n{passed}/{len(tests)} approval_signature tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
