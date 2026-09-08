"""
Cognitive heartbeat tests -- Phase 3 (Day 7).

brain/v2/heartbeat.py's CognitiveHeartbeat.run_cycle() and its wiring into
brain/v2/conversation.py's Alfred (start_heartbeat/stop_heartbeat/
get_heartbeat_log). All fakes -- no real LLM keys, no real memory backend, no
real network. Run directly:

    python build-system/test_heartbeat.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.heartbeat import CognitiveHeartbeat, _action_signature  # noqa: E402


class LLMResponse:
    """Local stand-in for brain.llm_router.LLMResponse's shape -- avoids
    importing llm_router.py, which pulls in groq/openai/google-genai SDKs at
    module level purely for class definitions this test never touches."""

    def __init__(self, text=None):
        self.text = text


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_PROFILE_CONTEXT = (
    "[MEMORY CONTEXT]\n\n## User Profile:\n\n### Goals\n"
    "- mit_essay_deadline: 2026-09-20, no progress logged in 2 weeks\n"
)


class FakeMemory:
    def __init__(self, context=_PROFILE_CONTEXT, raise_on_context=False):
        self._context = context
        self._raise = raise_on_context

    def get_context_for_llm(self, query=None):
        if self._raise:
            raise RuntimeError("vault unreachable")
        return self._context


class FakeRouter:
    """Returns queued raw text responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0
        self.last_call_kwargs = None

    async def call(self, **kwargs):
        self.call_count += 1
        self.last_call_kwargs = kwargs
        text = self._responses.pop(0) if self._responses else '{"gap_found": false}'
        return LLMResponse(text=text)


def make_alfred():
    """Alfred instance built without calling __init__ (which pulls in the
    real vault/DB/LLM clients) -- same pattern as test_speed_audit_timing.py's
    make_alfred(). Heartbeat-related attributes are set by hand since
    __init__ never ran to set them."""
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory()
    a._router = FakeRouter([])
    a._heartbeat = None
    a._heartbeat_task = None
    a._heartbeat_on_alert = None
    return a


# ---------------------------------------------------------------------------
# CognitiveHeartbeat.run_cycle() -- direct
# ---------------------------------------------------------------------------

async def _test_idle_when_no_profile_yet():
    hb = CognitiveHeartbeat(FakeMemory(context="[MEMORY CONTEXT]\n"), FakeRouter([]))
    entry = await hb.run_cycle()
    assert entry.type == "idle"
    assert hb.router.call_count == 0, "no profile/goals to check -- must not spend an LLM call"


async def _test_low_confidence_becomes_observation():
    router = FakeRouter(['{"gap_found": true, "confidence": "low", "observation": "minor thing"}'])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "observation"
    assert entry.confidence == "low"
    assert entry.observation == "minor thing"
    assert entry.action is None


async def _test_medium_confidence_becomes_nudge():
    router = FakeRouter(['{"gap_found": true, "confidence": "medium", "observation": "essay deadline slipping"}'])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "nudge"
    assert entry.confidence == "medium"
    assert entry.action is None


async def _test_high_confidence_with_action_becomes_proposal():
    router = FakeRouter([
        '{"gap_found": true, "confidence": "high", "observation": "no study block for Monday test",'
        ' "suggested_action": {"tool": "calendar", "params": {"action": "create", "summary": "Study block"}}}'
    ])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "proposal"
    assert entry.confidence == "high"
    assert entry.action == {"tool": "calendar", "params": {"action": "create", "summary": "Study block"}}
    assert entry.signature == _action_signature("calendar", {"action": "create", "summary": "Study block"})


async def _test_high_confidence_without_action_downgrades_to_nudge():
    """The manifesto's spec says confidence "high" implies a ready-to-run
    action -- if the model claims high confidence but gives nothing runnable,
    that is a broken contract, not a green light to fabricate one. Must not
    silently execute nothing while also silently dropping the observation."""
    router = FakeRouter(['{"gap_found": true, "confidence": "high", "observation": "something is off"}'])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "nudge"
    assert entry.confidence == "medium"
    assert entry.action is None


async def _test_no_gap_found_is_idle_regardless_of_confidence():
    router = FakeRouter(['{"gap_found": false, "confidence": "high", "observation": "ignored"}'])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "idle"


async def _test_malformed_llm_reply_becomes_error_not_a_crash():
    router = FakeRouter(["I refuse to answer in JSON today."])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    entry = await hb.run_cycle()
    assert entry.type == "error"


async def _test_llm_call_exception_becomes_error_not_a_crash():
    class BoomRouter:
        async def call(self, **kwargs):
            raise ConnectionError("no route to provider")

    hb = CognitiveHeartbeat(FakeMemory(), BoomRouter())
    entry = await hb.run_cycle()
    assert entry.type == "error"


async def _test_memory_exception_becomes_error_not_a_crash():
    hb = CognitiveHeartbeat(FakeMemory(raise_on_context=True), FakeRouter([]))
    entry = await hb.run_cycle()
    assert entry.type == "error"


async def _test_log_accumulates_across_cycles():
    router = FakeRouter([
        '{"gap_found": true, "confidence": "low", "observation": "one"}',
        '{"gap_found": true, "confidence": "medium", "observation": "two"}',
    ])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    await hb.run_cycle()
    await hb.run_cycle()
    log = hb.get_log()
    assert len(log) == 2
    assert log[0]["observation"] == "one"
    assert log[1]["observation"] == "two"


async def _test_prompt_asks_for_the_manifesto_json_shape():
    """Not a live-model check (this sandbox has none) -- just proves the
    actual request sent to the router asks for the confidence-gated JSON
    contract this module parses, and includes the real memory context
    rather than an empty/placeholder string."""
    router = FakeRouter(['{"gap_found": false}'])
    hb = CognitiveHeartbeat(FakeMemory(), router)
    await hb.run_cycle()
    kwargs = router.last_call_kwargs
    assert "gap_found" in kwargs["system_prompt"]
    assert "confidence" in kwargs["system_prompt"]
    assert "mit_essay_deadline" in kwargs["user_message"]


# ---------------------------------------------------------------------------
# Alfred.start_heartbeat() / stop_heartbeat() / get_heartbeat_log()
# ---------------------------------------------------------------------------

async def _test_constructing_alfred_never_starts_a_background_task():
    """Alfred() must never auto-start the heartbeat -- every existing
    build-system/test_*.py builds an Alfred via __new__()/__init__() with no
    idea this loop exists; an auto-started task would leak into every one of
    them."""
    a = make_alfred()
    assert a._heartbeat_task is None
    assert a.get_heartbeat_log() == []


async def _test_start_heartbeat_runs_a_cycle_and_fires_on_alert():
    a = make_alfred()
    a._router = FakeRouter(['{"gap_found": true, "confidence": "medium", "observation": "check in on X"}'])

    alerts = []

    async def on_alert(entry):
        alerts.append(entry)

    a.start_heartbeat(interval_seconds=0.01, on_alert=on_alert)
    try:
        await asyncio.sleep(0.15)
    finally:
        await a.stop_heartbeat()

    assert len(alerts) >= 1
    assert alerts[0]["type"] == "nudge"
    assert a.get_heartbeat_log()
    assert a._heartbeat_task is None, "stop_heartbeat must clear the task reference"


async def _test_start_heartbeat_is_a_noop_if_already_running():
    a = make_alfred()
    a.start_heartbeat(interval_seconds=10.0)
    first_task = a._heartbeat_task
    a.start_heartbeat(interval_seconds=10.0)
    assert a._heartbeat_task is first_task
    await a.stop_heartbeat()


async def _test_stop_heartbeat_before_start_is_a_safe_noop():
    a = make_alfred()
    await a.stop_heartbeat()  # must not raise
    assert a._heartbeat_task is None


async def _test_idle_cycles_do_not_trigger_on_alert():
    a = make_alfred()
    a._router = FakeRouter(['{"gap_found": false}'] * 5)

    alerts = []

    async def on_alert(entry):
        alerts.append(entry)

    a.start_heartbeat(interval_seconds=0.01, on_alert=on_alert)
    try:
        await asyncio.sleep(0.1)
    finally:
        await a.stop_heartbeat()

    assert alerts == [], "idle/observation cycles must not be pushed as alerts"


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain async defs
# wrapped in sync test_* functions, run directly, no pytest).
# ---------------------------------------------------------------------------

def test_idle_when_no_profile_yet():
    asyncio.run(_test_idle_when_no_profile_yet())


def test_low_confidence_becomes_observation():
    asyncio.run(_test_low_confidence_becomes_observation())


def test_medium_confidence_becomes_nudge():
    asyncio.run(_test_medium_confidence_becomes_nudge())


def test_high_confidence_with_action_becomes_proposal():
    asyncio.run(_test_high_confidence_with_action_becomes_proposal())


def test_high_confidence_without_action_downgrades_to_nudge():
    asyncio.run(_test_high_confidence_without_action_downgrades_to_nudge())


def test_no_gap_found_is_idle_regardless_of_confidence():
    asyncio.run(_test_no_gap_found_is_idle_regardless_of_confidence())


def test_malformed_llm_reply_becomes_error_not_a_crash():
    asyncio.run(_test_malformed_llm_reply_becomes_error_not_a_crash())


def test_llm_call_exception_becomes_error_not_a_crash():
    asyncio.run(_test_llm_call_exception_becomes_error_not_a_crash())


def test_memory_exception_becomes_error_not_a_crash():
    asyncio.run(_test_memory_exception_becomes_error_not_a_crash())


def test_log_accumulates_across_cycles():
    asyncio.run(_test_log_accumulates_across_cycles())


def test_prompt_asks_for_the_manifesto_json_shape():
    asyncio.run(_test_prompt_asks_for_the_manifesto_json_shape())


def test_constructing_alfred_never_starts_a_background_task():
    asyncio.run(_test_constructing_alfred_never_starts_a_background_task())


def test_start_heartbeat_runs_a_cycle_and_fires_on_alert():
    asyncio.run(_test_start_heartbeat_runs_a_cycle_and_fires_on_alert())


def test_start_heartbeat_is_a_noop_if_already_running():
    asyncio.run(_test_start_heartbeat_is_a_noop_if_already_running())


def test_stop_heartbeat_before_start_is_a_safe_noop():
    asyncio.run(_test_stop_heartbeat_before_start_is_a_safe_noop())


def test_idle_cycles_do_not_trigger_on_alert():
    asyncio.run(_test_idle_cycles_do_not_trigger_on_alert())


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
    print(f"\n{passed}/{len(tests)} heartbeat tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
