"""
Phase 3 "relocated heartbeat" tests — ROADMAP.md Week 3.

brain/v2/heartbeat.py ports the v1 cognitive heartbeat (dead code under the
v2 rebuild -- it called memory.t3_search()/db.get_due_reminders(), neither
of which exist on the current FiveTierMemory/LocalDB) into the current
architecture, and upgrades it per docs/MANIFESTO_V5.md section 3: a
periodic LLM call that compares Sam's stored profile/goals against recent
activity, classifies its own confidence, and gates what happens next on
that confidence (high -> proposed action alert, medium -> nudge alert,
low -> logged only, no alert).

All fakes, no network/LLM keys, no real vault, no real event loop timers
(interval sleeps are never exercised here -- tests call Heartbeat.tick()
directly instead of waiting out DEFAULT_INTERVAL_SECONDS). Run directly:

    python build-system/test_heartbeat.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.heartbeat import (  # noqa: E402
    Heartbeat,
    PulseResult,
    _alert_from_pulse,
    _extract_json_object,
    run_cognitive_pulse,
)


class LLMResponse:
    """Local stand-in for brain.llm_router.LLMResponse's shape -- avoids
    importing llm_router.py itself, which pulls in the groq/openai/
    google-genai SDKs at module level purely for class definitions this
    test never touches (same rationale as test_speed_audit_timing.py)."""

    def __init__(self, text=None):
        self.text = text


class FakeMemory:
    def __init__(self, profile_context="", episodes=None):
        self._profile_context = profile_context
        self._episodes = episodes or []

    def get_context_for_llm(self, query=None):
        return self._profile_context

    def t3_find_episodes(self, query, max_results=5):
        return self._episodes


class FakeRouter:
    def __init__(self, reply_text=None, raise_error=False):
        self._reply_text = reply_text
        self._raise_error = raise_error
        self.call_count = 0
        self.last_call_kwargs = None

    async def call(self, system_prompt, user_message, **kwargs):
        self.call_count += 1
        self.last_call_kwargs = {"system_prompt": system_prompt, "user_message": user_message, **kwargs}
        if self._raise_error:
            raise RuntimeError("simulated provider outage")
        return LLMResponse(text=self._reply_text)


class FakeAlfred:
    """Just enough of Alfred's surface for run_cognitive_pulse()/Heartbeat --
    memory, _router, and the _pending_alerts list get_and_clear_alerts()
    would normally own."""

    def __init__(self, memory, router):
        self.memory = memory
        self._router = router
        self._pending_alerts = []


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------

async def _test_extract_json_object_plain():
    obj = _extract_json_object('{"confidence": "low", "observation": "fine"}')
    assert obj == {"confidence": "low", "observation": "fine"}


async def _test_extract_json_object_wrapped_in_prose():
    text = 'Sure, here you go:\n{"confidence": "medium", "observation": "x"}\nHope that helps.'
    obj = _extract_json_object(text)
    assert obj == {"confidence": "medium", "observation": "x"}


async def _test_extract_json_object_none_when_absent():
    assert _extract_json_object("not json at all") is None


# ---------------------------------------------------------------------------
# run_cognitive_pulse
# ---------------------------------------------------------------------------

async def _test_pulse_skips_llm_call_with_nothing_to_reason_about():
    alfred = FakeAlfred(FakeMemory(profile_context="", episodes=[]), FakeRouter())
    result = await run_cognitive_pulse(alfred)
    assert result is None
    assert alfred._router.call_count == 0, "must not spend an LLM call with no context at all"


async def _test_pulse_parses_high_confidence_with_action():
    memory = FakeMemory(
        profile_context="[MEMORY CONTEXT]\n## User Profile:\n### Goals\n- mit_essay_deadline: 2026-09-10",
        episodes=[{"title": "no essay progress logged", "snippet": "3 days, zero writing"}],
    )
    router = FakeRouter(reply_text=(
        '{"confidence": "high", "observation": "MIT essay deadline in 2 days with no progress.", '
        '"action": "Draft an outline tonight."}'
    ))
    alfred = FakeAlfred(memory, router)
    result = await run_cognitive_pulse(alfred)
    assert isinstance(result, PulseResult)
    assert result.confidence == "high"
    assert "MIT essay" in result.observation
    assert result.action == "Draft an outline tonight."
    assert router.call_count == 1
    assert "recent activity" in router.last_call_kwargs["user_message"] or "Goals" in router.last_call_kwargs["user_message"]


async def _test_pulse_parses_low_confidence_without_action():
    memory = FakeMemory(profile_context="## User Profile:\n- likes: chess")
    router = FakeRouter(reply_text='{"confidence": "low", "observation": "Nothing notable.", "action": null}')
    alfred = FakeAlfred(memory, router)
    result = await run_cognitive_pulse(alfred)
    assert result.confidence == "low"
    assert result.action is None


async def _test_pulse_returns_none_on_unparseable_reply():
    memory = FakeMemory(profile_context="## User Profile:\n- x: y")
    router = FakeRouter(reply_text="I'm not sure how to answer that.")
    alfred = FakeAlfred(memory, router)
    result = await run_cognitive_pulse(alfred)
    assert result is None


async def _test_pulse_returns_none_on_bad_confidence_value():
    memory = FakeMemory(profile_context="## User Profile:\n- x: y")
    router = FakeRouter(reply_text='{"confidence": "extremely high", "observation": "x"}')
    alfred = FakeAlfred(memory, router)
    result = await run_cognitive_pulse(alfred)
    assert result is None


async def _test_pulse_fails_silently_when_router_raises():
    memory = FakeMemory(profile_context="## User Profile:\n- x: y")
    router = FakeRouter(raise_error=True)
    alfred = FakeAlfred(memory, router)
    result = await run_cognitive_pulse(alfred)
    assert result is None, "a provider outage must never crash the heartbeat loop"


async def _test_pulse_fails_silently_when_memory_raises():
    class ExplodingMemory:
        def get_context_for_llm(self, query=None):
            raise RuntimeError("vault unreachable")

        def t3_find_episodes(self, query, max_results=5):
            raise RuntimeError("index unreachable")

    alfred = FakeAlfred(ExplodingMemory(), FakeRouter(reply_text='{"confidence": "low", "observation": "x"}'))
    result = await run_cognitive_pulse(alfred)
    assert result is None
    assert alfred._router.call_count == 0, "no context could be built, so no LLM call should fire"


# ---------------------------------------------------------------------------
# _alert_from_pulse — confidence gating
# ---------------------------------------------------------------------------

async def _test_alert_gating_low_produces_no_alert():
    assert _alert_from_pulse(PulseResult(confidence="low", observation="fine")) is None


async def _test_alert_gating_medium_produces_nudge():
    alert = _alert_from_pulse(PulseResult(confidence="medium", observation="worth a nudge"))
    assert alert["type"] == "heartbeat_nudge"
    assert alert["confidence"] == "medium"
    assert "proposed_action" not in alert


async def _test_alert_gating_high_produces_action_proposal_not_executed():
    alert = _alert_from_pulse(PulseResult(confidence="high", observation="urgent gap", action="Send the email now"))
    assert alert["type"] == "heartbeat_action_proposal"
    assert alert["proposed_action"] == "Send the email now"
    # The whole point: this is a proposal, not a record of something run.
    assert "executed" not in alert and "result" not in alert


# ---------------------------------------------------------------------------
# Heartbeat.tick()
# ---------------------------------------------------------------------------

async def _test_tick_appends_alert_and_invokes_callback():
    memory = FakeMemory(
        profile_context="## User Profile:\n- goal: ship phase 3",
        episodes=[{"title": "quiet week", "snippet": "no commits in 5 days"}],
    )
    router = FakeRouter(reply_text='{"confidence": "medium", "observation": "Quiet week on phase 3.", "action": null}')
    alfred = FakeAlfred(memory, router)

    received = []

    async def on_alert(alert):
        received.append(alert)

    hb = Heartbeat(alfred, on_alert=on_alert)
    result = await hb.tick()

    assert result.confidence == "medium"
    assert hb.pulse_count == 1
    assert hb.last_result is result
    assert len(alfred._pending_alerts) == 1
    assert alfred._pending_alerts[0]["observation"] == "Quiet week on phase 3."
    assert received == alfred._pending_alerts, "on_alert must receive the same alert that was queued"


async def _test_tick_low_confidence_counts_but_no_alert():
    memory = FakeMemory(profile_context="## User Profile:\n- goal: x")
    router = FakeRouter(reply_text='{"confidence": "low", "observation": "all quiet", "action": null}')
    alfred = FakeAlfred(memory, router)

    hb = Heartbeat(alfred)
    result = await hb.tick()

    assert result.confidence == "low"
    assert hb.pulse_count == 1
    assert alfred._pending_alerts == []


async def _test_tick_skips_outside_active_hours():
    memory = FakeMemory(profile_context="## User Profile:\n- goal: x")
    router = FakeRouter(reply_text='{"confidence": "high", "observation": "x", "action": "y"}')
    alfred = FakeAlfred(memory, router)

    hb = Heartbeat(alfred, active_hours=range(0, 0))  # never active
    result = await hb.tick()

    assert result is None
    assert hb.pulse_count == 0, "an inactive-hours skip must not count as a real pulse"
    assert router.call_count == 0
    assert alfred._pending_alerts == []


async def _test_tick_on_alert_exception_does_not_propagate():
    memory = FakeMemory(profile_context="## User Profile:\n- goal: x")
    router = FakeRouter(reply_text='{"confidence": "medium", "observation": "x", "action": null}')
    alfred = FakeAlfred(memory, router)

    async def broken_on_alert(alert):
        raise RuntimeError("websocket broadcast failed")

    hb = Heartbeat(alfred, on_alert=broken_on_alert)
    result = await hb.tick()  # must not raise

    assert result.confidence == "medium"
    assert len(alfred._pending_alerts) == 1, "the alert must still be queued even if the live push failed"


# ---------------------------------------------------------------------------
# Heartbeat.start()/stop() lifecycle
# ---------------------------------------------------------------------------

async def _test_start_creates_task_and_stop_cancels_it():
    memory = FakeMemory(profile_context="")
    router = FakeRouter()
    alfred = FakeAlfred(memory, router)

    hb = Heartbeat(alfred, interval_seconds=3600)
    hb.start()
    assert hb._task is not None
    assert hb.enabled is True

    hb.stop()
    assert hb.enabled is False
    assert hb._task is None
    await asyncio.sleep(0)  # let the cancellation land


async def _test_start_is_idempotent():
    alfred = FakeAlfred(FakeMemory(), FakeRouter())
    hb = Heartbeat(alfred)
    hb.start()
    first_task = hb._task
    hb.start()
    assert hb._task is first_task, "a second start() must not replace the running task"
    hb.stop()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Alfred integration (start_heartbeat/stop_heartbeat/get_and_clear_alerts)
# ---------------------------------------------------------------------------

async def _test_alfred_heartbeat_lifecycle():
    from brain.v2.conversation import Alfred

    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory(profile_context="")
    a._router = FakeRouter()
    a._pending_alerts = []
    a._heartbeat = None

    a.start_heartbeat(interval_seconds=3600)
    assert a._heartbeat is not None
    heartbeat_ref = a._heartbeat

    a.start_heartbeat(interval_seconds=60)  # must no-op, not replace
    assert a._heartbeat is heartbeat_ref

    a._pending_alerts.append({"type": "heartbeat_nudge", "observation": "x"})
    alerts = a.get_and_clear_alerts()
    assert alerts == [{"type": "heartbeat_nudge", "observation": "x"}]
    assert a.get_and_clear_alerts() == [], "alerts must be cleared once returned"

    a.stop_heartbeat()
    assert a._heartbeat is None
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain async defs
# named test_*, run directly, no pytest).
# ---------------------------------------------------------------------------

def test_extract_json_object_plain():
    asyncio.run(_test_extract_json_object_plain())


def test_extract_json_object_wrapped_in_prose():
    asyncio.run(_test_extract_json_object_wrapped_in_prose())


def test_extract_json_object_none_when_absent():
    asyncio.run(_test_extract_json_object_none_when_absent())


def test_pulse_skips_llm_call_with_nothing_to_reason_about():
    asyncio.run(_test_pulse_skips_llm_call_with_nothing_to_reason_about())


def test_pulse_parses_high_confidence_with_action():
    asyncio.run(_test_pulse_parses_high_confidence_with_action())


def test_pulse_parses_low_confidence_without_action():
    asyncio.run(_test_pulse_parses_low_confidence_without_action())


def test_pulse_returns_none_on_unparseable_reply():
    asyncio.run(_test_pulse_returns_none_on_unparseable_reply())


def test_pulse_returns_none_on_bad_confidence_value():
    asyncio.run(_test_pulse_returns_none_on_bad_confidence_value())


def test_pulse_fails_silently_when_router_raises():
    asyncio.run(_test_pulse_fails_silently_when_router_raises())


def test_pulse_fails_silently_when_memory_raises():
    asyncio.run(_test_pulse_fails_silently_when_memory_raises())


def test_alert_gating_low_produces_no_alert():
    asyncio.run(_test_alert_gating_low_produces_no_alert())


def test_alert_gating_medium_produces_nudge():
    asyncio.run(_test_alert_gating_medium_produces_nudge())


def test_alert_gating_high_produces_action_proposal_not_executed():
    asyncio.run(_test_alert_gating_high_produces_action_proposal_not_executed())


def test_tick_appends_alert_and_invokes_callback():
    asyncio.run(_test_tick_appends_alert_and_invokes_callback())


def test_tick_low_confidence_counts_but_no_alert():
    asyncio.run(_test_tick_low_confidence_counts_but_no_alert())


def test_tick_skips_outside_active_hours():
    asyncio.run(_test_tick_skips_outside_active_hours())


def test_tick_on_alert_exception_does_not_propagate():
    asyncio.run(_test_tick_on_alert_exception_does_not_propagate())


def test_start_creates_task_and_stop_cancels_it():
    asyncio.run(_test_start_creates_task_and_stop_cancels_it())


def test_start_is_idempotent():
    asyncio.run(_test_start_is_idempotent())


def test_alfred_heartbeat_lifecycle():
    asyncio.run(_test_alfred_heartbeat_lifecycle())


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
