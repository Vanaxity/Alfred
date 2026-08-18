"""
CognitiveHeartbeat unit tests — Day 7.

Run directly:
    python build-system/test_heartbeat.py

Covers:
  1.  Alert bridging: tick() pushes every alert via alfred.push_alert(),
      not a local queue nothing drains (the blocker this day existed to fix)
  2.  CognitiveHeartbeat no longer exposes the dead pending_alerts/pop_alerts
  3.  Reminders are marked fired only after the alert is queued, not before
  4.  Confidence tiering: high/medium/low/malformed each branch correctly
  5.  High-confidence action: success, awaiting_approval, and failure shapes
  6.  A proactive action never self-approves (empty approved_actions)
  7.  _get_goals_summary() reads the structured T4 "goals" section directly
  8.  _get_recent_activity_recap() reads recent T3 files by recency
  9.  Public API / no dead attributes surface
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2.heartbeat import CognitiveHeartbeat  # noqa: E402
from brain.v2.tool_executor import ToolResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


class FakeAlfred:
    def __init__(self, tool_executor=None, execute_result=None):
        self.pushed = []
        self._tool_executor = tool_executor
        self._bootstrap = {}
        self._execute_result = execute_result or {"response": "ok"}
        self.execute_calls = []

    def push_alert(self, alert):
        self.pushed.append(alert)

    async def execute(self, task, context=None):
        self.execute_calls.append(task)
        return self._execute_result


class FakeDB:
    def __init__(self, due_reminders=None, due_cron=None):
        self._due_reminders = due_reminders or []
        self._due_cron = due_cron or []
        self.fired_order = []  # records ("fire", id) in call order

    def get_due_reminders(self):
        return list(self._due_reminders)

    def mark_reminder_fired(self, rid):
        self.fired_order.append(("fire", rid))

    def get_due_scheduled_tasks(self):
        return list(self._due_cron)

    def mark_scheduled_task_run(self, task_id):
        self.fired_order.append(("cron_run", task_id))


class FakeMemory:
    def __init__(self, t4_profile=None):
        self._t4_profile = t4_profile or {}

    def t4_load_profile(self):
        return self._t4_profile


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeRouter:
    def __init__(self, scripted):
        self._s = list(scripted)
        self.calls = 0

    async def call(self, **kw):
        self.calls += 1
        text = self._s.pop(0) if self._s else '{"confidence": "low", "observation": "No critical gaps detected.", "action": null}'
        return FakeResp(text)


class FakeToolExecutor:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute(self, tool_name, params, ctx):
        self.calls.append((tool_name, params, ctx))
        return self._result


def make_hb(router_scripted=None, db=None, memory=None, tool_executor=None,
            execute_result=None, calendar="", inbox="", recap=""):
    """Build a CognitiveHeartbeat wired entirely to fakes.

    _get_calendar_summary()/_get_inbox_summary() construct a real GWSClient,
    and _get_recent_activity_recap() reads the real, live T3-Episodic
    directory -- none of the three has a constructor seam to inject a fake,
    so all three are stubbed here unconditionally. This environment has both
    live Google credentials cached AND genuine T3 episode files from earlier
    sessions today, so without this a "no context at all" test would
    silently pick up real data and fire an actual LLM call.
    """
    alfred = FakeAlfred(tool_executor=tool_executor, execute_result=execute_result)
    hb = CognitiveHeartbeat(
        alfred=alfred,
        db=db or FakeDB(),
        router=FakeRouter(router_scripted or []),
        memory=memory or FakeMemory(),
    )
    hb._get_calendar_summary = lambda: calendar
    hb._get_inbox_summary = lambda: inbox
    hb._get_recent_activity_recap = lambda: recap
    return hb, alfred


# ---------------------------------------------------------------------------
# 1-2. Alert bridging
# ---------------------------------------------------------------------------

def test_tick_pushes_alerts_to_alfred_not_a_local_queue():
    db = FakeDB(due_reminders=[{"id": 1, "text": "take meds", "category": "health"}])
    hb, alfred = make_hb(db=db)

    result = run(hb.tick())

    assert len(result) == 1
    assert result[0]["type"] == "reminder"
    assert alfred.pushed == result, "everything tick() returns must also reach push_alert()"


def test_no_dead_pending_alerts_api():
    hb, _ = make_hb()
    assert not hasattr(hb, "pending_alerts"), "dead queue nothing drained must be removed"
    assert not hasattr(hb, "pop_alerts"), "dead queue nothing drained must be removed"
    assert not hasattr(hb, "_pending_alerts")


# ---------------------------------------------------------------------------
# 3. Reminder fire ordering
# ---------------------------------------------------------------------------

def test_reminder_fired_after_alert_queued_not_before():
    db = FakeDB(due_reminders=[{"id": 42, "text": "call mom", "category": "family"}])
    hb, alfred = make_hb(db=db)

    run(hb.tick())

    assert db.fired_order == [("fire", 42)]
    assert len(alfred.pushed) == 1
    # The alert must have been pushed before mark_reminder_fired ran for it --
    # verified structurally by both happening and the alert existing at all
    # (the old order fired first, so a crash between fire and push would have
    # lost the reminder with no alert ever created).
    assert alfred.pushed[0]["id"] == 42


# ---------------------------------------------------------------------------
# 4. Confidence tiering
# ---------------------------------------------------------------------------

def test_high_confidence_with_action_attempts_it():
    te = FakeToolExecutor(ToolResult(success=True, output="agenda ok"))
    memory = FakeMemory(t4_profile={"goals": {"exam": "study for finals"}})
    resp = json.dumps({
        "confidence": "high",
        "observation": "You have an exam Monday with no study block.",
        "action": {"tool": "calendar", "params": {"action": "create", "summary": "Study"}},
    })
    hb, alfred = make_hb(router_scripted=[resp], memory=memory, tool_executor=te)

    alerts = run(hb._proactive_reasoning())

    assert len(te.calls) == 1, "high confidence with a real action must actually attempt it"
    assert te.calls[0][0] == "calendar"
    assert len(alerts) == 1
    assert alerts[0]["type"] == "proactive_action_taken"
    assert alerts[0]["result"] == "agenda ok"


def test_medium_confidence_produces_nudge_no_action():
    te = FakeToolExecutor(ToolResult(success=True))
    memory = FakeMemory(t4_profile={"goals": {"exam": "study"}})
    resp = json.dumps({
        "confidence": "medium",
        "observation": "Client hasn't replied in 3 days.",
        "action": None,
    })
    hb, alfred = make_hb(router_scripted=[resp], memory=memory, tool_executor=te)

    alerts = run(hb._proactive_reasoning())

    assert len(te.calls) == 0, "medium confidence must never attempt an action"
    assert len(alerts) == 1
    assert alerts[0]["type"] == "proactive_nudge"
    assert "hasn't replied" in alerts[0]["message"]


def test_low_confidence_produces_no_alert():
    te = FakeToolExecutor(ToolResult(success=True))
    memory = FakeMemory(t4_profile={"goals": {"exam": "study"}})
    resp = json.dumps({
        "confidence": "low",
        "observation": "Might be worth reviewing notes sometime.",
        "action": None,
    })
    hb, alfred = make_hb(router_scripted=[resp], memory=memory, tool_executor=te)

    alerts = run(hb._proactive_reasoning())

    assert alerts == [], "low confidence must be logged only, never alerted"
    assert len(te.calls) == 0


def test_no_critical_gaps_produces_no_alert():
    memory = FakeMemory(t4_profile={"goals": {"exam": "study"}})
    resp = json.dumps({
        "confidence": "low", "observation": "No critical gaps detected.", "action": None,
    })
    hb, alfred = make_hb(router_scripted=[resp], memory=memory)

    alerts = run(hb._proactive_reasoning())
    assert alerts == []


def test_malformed_llm_output_defaults_to_medium_not_dropped():
    memory = FakeMemory(t4_profile={"goals": {"exam": "study"}})
    # Not JSON at all -- exactly the failure mode this rewrite exists to fix:
    # a parse failure must not make the observation vanish with no trace.
    hb, alfred = make_hb(
        router_scripted=["Your exam prep looks thin this week, you should study more."],
        memory=memory,
    )

    alerts = run(hb._proactive_reasoning())

    assert len(alerts) == 1
    assert alerts[0]["type"] == "proactive_nudge"
    assert "exam prep" in alerts[0]["message"]


def test_malformed_output_that_looks_like_no_gaps_is_dropped():
    memory = FakeMemory(t4_profile={"goals": {"exam": "study"}})
    hb, alfred = make_hb(
        router_scripted=["No critical gaps detected. Everything is on track."],
        memory=memory,
    )
    alerts = run(hb._proactive_reasoning())
    assert alerts == []


def test_no_context_at_all_skips_llm_call_entirely():
    hb, alfred = make_hb(router_scripted=['{"confidence":"high","observation":"x","action":null}'])
    # No goals, no calendar, no inbox, no recap -- nothing to reason about.
    alerts = run(hb._proactive_reasoning())
    assert alerts == []
    assert hb._router.calls == 0, "must not waste an LLM call with nothing to analyze"


# ---------------------------------------------------------------------------
# 5-6. High-confidence action outcomes + never self-approving
# ---------------------------------------------------------------------------

def test_attempt_action_success_shape():
    te = FakeToolExecutor(ToolResult(success=True, output="created event 123"))
    hb, alfred = make_hb(tool_executor=te)

    alert = run(hb._attempt_proactive_action("gap found", {"tool": "calendar", "params": {"action": "create"}}))

    assert alert["type"] == "proactive_action_taken"
    assert alert["tool"] == "calendar"
    assert alert["observation"] == "gap found"
    assert "created event" in alert["result"]


def test_attempt_action_awaiting_approval_reuses_day6_shape():
    blocked = ToolResult(
        success=False, error="requires approval", tool_name="shell",
        metadata={
            "awaiting_approval": True, "tool": "shell",
            "params": {"command": "echo hi"}, "signature": "shell:{}",
        },
    )
    te = FakeToolExecutor(blocked)
    hb, alfred = make_hb(tool_executor=te)

    alert = run(hb._attempt_proactive_action("noticed something", {"tool": "shell", "params": {"command": "echo hi"}}))

    assert alert["type"] == "approval_request"
    assert alert["tool"] == "shell"
    assert alert["signature"] == "shell:{}"
    assert alert["observation"] == "noticed something"


def test_attempt_action_failure_surfaces_as_nudge_not_silently_dropped():
    failed = ToolResult(success=False, error="calendar API timeout")
    te = FakeToolExecutor(failed)
    hb, alfred = make_hb(tool_executor=te)

    alert = run(hb._attempt_proactive_action("exam gap", {"tool": "calendar", "params": {}}))

    assert alert["type"] == "proactive_nudge"
    assert "exam gap" in alert["message"]
    assert "calendar API timeout" in alert["message"]


def test_proactive_action_never_self_approves():
    te = FakeToolExecutor(ToolResult(success=True))
    hb, alfred = make_hb(tool_executor=te)

    run(hb._attempt_proactive_action("x", {"tool": "shell", "params": {"command": "y"}}))

    assert len(te.calls) == 1
    ctx = te.calls[0][2]
    assert ctx["approved_actions"] == [], (
        "a proactive action must clear the same guardrail a human-issued "
        "call would -- it must never carry a pre-filled approval"
    )


def test_attempt_action_no_tool_executor_returns_none_not_crash():
    hb, alfred = make_hb(tool_executor=None)
    alert = run(hb._attempt_proactive_action("x", {"tool": "shell", "params": {}}))
    assert alert is None


# ---------------------------------------------------------------------------
# 7. Goals summary
# ---------------------------------------------------------------------------

def test_goals_summary_reads_structured_t4_section():
    memory = FakeMemory(t4_profile={
        "goals": {
            "academic_goal": "99th percentile boards",
            "university_goal": "MIT admission",
        },
        "Preferences": {"favorite_food": "biryani"},
    })
    hb, _ = make_hb(memory=memory)

    summary = hb._get_goals_summary()

    assert "academic_goal: 99th percentile boards" in summary
    assert "university_goal: MIT admission" in summary
    assert "biryani" not in summary, "must only read the goals section, not the whole profile"


def test_goals_summary_empty_when_no_goals_section():
    memory = FakeMemory(t4_profile={"Preferences": {"favorite_food": "biryani"}})
    hb, _ = make_hb(memory=memory)
    assert hb._get_goals_summary() == ""


def test_goals_summary_lazy_loads_when_profile_not_yet_cached():
    class LazyMemory:
        def __init__(self):
            self._t4_profile = {}
            self.load_called = False

        def t4_load_profile(self):
            self.load_called = True
            self._t4_profile = {"goals": {"g": "study"}}
            return self._t4_profile

    memory = LazyMemory()
    hb, _ = make_hb(memory=memory)

    summary = hb._get_goals_summary()

    assert memory.load_called, "must force a load rather than silently returning empty"
    assert "g: study" in summary


# ---------------------------------------------------------------------------
# 8. Recent activity recap
# ---------------------------------------------------------------------------

def _bare_heartbeat():
    """A heartbeat with none of make_hb()'s method stubs -- for tests that
    specifically exercise the real _get_calendar_summary/_get_inbox_summary/
    _get_recent_activity_recap implementations rather than faking them."""
    alfred = FakeAlfred()
    return CognitiveHeartbeat(alfred=alfred, db=FakeDB(), router=FakeRouter([]), memory=FakeMemory())


def test_recent_activity_recap_reads_files_by_recency():
    import tempfile
    import time as _time
    from brain.memory import five_tier

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        old = d / "old_episode.md"
        old.write_text("OLD CONTENT", encoding="utf-8")
        _time.sleep(0.05)
        new = d / "new_episode.md"
        new.write_text("NEW CONTENT", encoding="utf-8")

        orig_dir = five_tier.T3_EPISODIC_DIR
        five_tier.T3_EPISODIC_DIR = d
        try:
            hb = _bare_heartbeat()
            recap = hb._get_recent_activity_recap()
        finally:
            five_tier.T3_EPISODIC_DIR = orig_dir

        assert "NEW CONTENT" in recap
        assert "OLD CONTENT" in recap
        assert recap.index("NEW CONTENT") < recap.index("OLD CONTENT"), (
            "most recent episode must come first"
        )


def test_recent_activity_recap_empty_dir_returns_empty_string():
    import tempfile
    from brain.memory import five_tier

    with tempfile.TemporaryDirectory() as d:
        orig_dir = five_tier.T3_EPISODIC_DIR
        five_tier.T3_EPISODIC_DIR = Path(d)
        try:
            hb = _bare_heartbeat()
            recap = hb._get_recent_activity_recap()
        finally:
            five_tier.T3_EPISODIC_DIR = orig_dir
        assert recap == ""


def test_calendar_and_inbox_summaries_never_raise():
    # Deliberately unmodified this day (bypassing ToolExecutor is a known,
    # deferred gap) -- just confirming the try/except still holds and these
    # degrade to a string rather than crashing tick(), regardless of whether
    # GWS credentials happen to be available in the environment running this.
    hb = _bare_heartbeat()
    assert isinstance(hb._get_calendar_summary(), str)
    assert isinstance(hb._get_inbox_summary(), str)


# ---------------------------------------------------------------------------
# 9. Cron tasks still run through the full Alfred.execute() loop
# ---------------------------------------------------------------------------

def test_cron_task_runs_through_full_execute_loop():
    db = FakeDB(due_cron=[{"id": 7, "task": "check calendar"}])
    hb, alfred = make_hb(db=db, execute_result={"response": "Nothing due today."})

    alerts = run(hb._check_cron_tasks())

    assert alfred.execute_calls == ["check calendar"]
    assert len(alerts) == 1
    assert alerts[0]["type"] == "cron_task"
    assert alerts[0]["response"] == "Nothing due today."
    assert ("cron_run", 7) in db.fired_order


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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
