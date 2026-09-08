"""
CognitiveHeartbeat tests — Day 7.

Two groups:
  1. LocalDB reminder/scheduled-task CRUD -- brand new methods (the old
     brain/alfred.py heartbeat called `get_due_reminders()` and
     `get_due_scheduled_tasks()`, but neither ever existed anywhere in this
     codebase; confirmed by grep before writing this). Uses a real
     temp-file SQLite DB, no mocking -- the due-time/cron-window logic is
     exactly what would be wrong in a mocked version.
  2. CognitiveHeartbeat mechanics (tick/start/stop/pop_alerts) against a
     FakeAlfred double -- no real DB, no real LLM, no real network.

No real API keys, no real vault, no real network calls. Run directly:

    python build-system/test_heartbeat.py
"""

import asyncio
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.local_db import LocalDB  # noqa: E402
from brain.v2.heartbeat import CognitiveHeartbeat  # noqa: E402
from brain.v2.conversation import Alfred  # noqa: E402


# ---------------------------------------------------------------------------
# Group 1: LocalDB reminders + scheduled tasks
# ---------------------------------------------------------------------------

def _fresh_db() -> LocalDB:
    tmpdir = tempfile.mkdtemp()
    return LocalDB(db_path=Path(tmpdir) / "test_heartbeat.db")


def test_reminder_due_when_past_and_not_fired():
    db = _fresh_db()
    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    rid = db.add_reminder("call mom", past)

    due = db.get_due_reminders()
    assert [r["id"] for r in due] == [rid]
    assert due[0]["text"] == "call mom"


def test_reminder_not_due_when_in_future():
    db = _fresh_db()
    future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.add_reminder("future thing", future)

    assert db.get_due_reminders() == []


def test_mark_reminder_fired_removes_it_from_due():
    db = _fresh_db()
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    rid = db.add_reminder("water the plants", past)

    assert len(db.get_due_reminders()) == 1
    db.mark_reminder_fired(rid)
    assert db.get_due_reminders() == []


def test_scheduled_task_due_when_created_well_in_the_past():
    db = _fresh_db()
    tid = db.add_scheduled_task("check calendar for conflicts", "* * * * *")
    # created_at defaults to "now" -- backdate it so the every-minute cron
    # expression clearly has a fire time before now, deterministically
    # (right at insert time, the next `* * * * *` fire could be up to 59s
    # in the future, which would make this test flaky).
    old = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE scheduled_tasks SET created_at = ? WHERE id = ?", (old, tid))
    conn.commit()

    due = db.get_due_scheduled_tasks()
    assert [t["id"] for t in due] == [tid]


def test_scheduled_task_not_due_right_after_running():
    db = _fresh_db()
    tid = db.add_scheduled_task("daily briefing", "* * * * *")
    old = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE scheduled_tasks SET created_at = ? WHERE id = ?", (old, tid))
    conn.commit()
    assert len(db.get_due_scheduled_tasks()) == 1

    db.update_last_run(tid)
    # last_run is "just now" -- the next `* * * * *` fire is up to 60s away,
    # so it must not be due immediately after running.
    assert db.get_due_scheduled_tasks() == []


def test_inactive_scheduled_task_never_due():
    db = _fresh_db()
    tid = db.add_scheduled_task("archived task", "* * * * *")
    old = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE scheduled_tasks SET created_at = ? WHERE id = ?", (old, tid))
    conn.commit()

    db.set_scheduled_task_active(tid, False)
    assert db.get_due_scheduled_tasks() == []
    assert db.get_scheduled_tasks(active_only=True) == []
    assert len(db.get_scheduled_tasks(active_only=False)) == 1


def test_malformed_cron_expression_is_skipped_not_raised():
    db = _fresh_db()
    tid = db.add_scheduled_task("broken task", "not-a-cron-expression")
    old = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE scheduled_tasks SET created_at = ? WHERE id = ?", (old, tid))
    conn.commit()

    # Must not raise -- one bad row must not block the due-task check.
    assert db.get_due_scheduled_tasks() == []


# ---------------------------------------------------------------------------
# Group 2: CognitiveHeartbeat mechanics
# ---------------------------------------------------------------------------

class LLMResponse:
    """Local stand-in for llm_router.LLMResponse's shape, same pattern as
    test_speed_audit_timing.py -- avoids importing llm_router.py itself."""

    def __init__(self, text=None):
        self.text = text


class FakeDB:
    def __init__(self, reminders=None, cron_due=None):
        self.reminders = reminders or []
        self.cron_due = cron_due or []
        self.fired_ids = []
        self.last_run_ids = []

    def get_due_reminders(self):
        return self.reminders

    def mark_reminder_fired(self, reminder_id):
        self.fired_ids.append(reminder_id)

    def get_due_scheduled_tasks(self):
        return self.cron_due

    def update_last_run(self, task_id):
        self.last_run_ids.append(task_id)


class FakeMemory:
    def get_context_for_llm(self, query=None):
        return "## User Profile:\nLikes turtles."


class FakeRouter:
    def __init__(self, text):
        self._text = text
        self.calls = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(text=self._text)


# A bare real Alfred instance, used only for its (stateless-enough) JSON
# reply parser -- reusing the real parsing logic rather than duplicating
# it, same as brain/v2/heartbeat.py itself does at runtime.
_PARSER = Alfred.__new__(Alfred)


class FakeAlfred:
    def __init__(self, db=None, memory=None, router=None, execute_fn=None):
        self.db = db
        self.memory = memory
        self._router = router
        self._execute_fn = execute_fn or self._default_execute
        self.execute_calls = []

    async def execute(self, task, context):
        self.execute_calls.append(task)
        return await self._execute_fn(task, context)

    @staticmethod
    async def _default_execute(task, context):
        return {"response": f"did: {task}"}

    def _parse_llm_output(self, content):
        return _PARSER._parse_llm_output(content)


def _run(coro):
    return asyncio.run(coro)


def test_check_reminders_fires_due_and_marks_fired():
    db = FakeDB(reminders=[{"id": 1, "text": "call mom"}])
    hb = CognitiveHeartbeat(FakeAlfred(db=db))

    alerts = hb._check_reminders()

    assert alerts == [{
        "type": "heartbeat", "source": "reminder",
        "content": "call mom", "reminder_id": 1,
    }]
    assert db.fired_ids == [1]


def test_check_reminders_with_no_db_returns_empty():
    hb = CognitiveHeartbeat(FakeAlfred(db=None))
    assert hb._check_reminders() == []


def test_run_due_cron_tasks_executes_through_alfred_and_updates_last_run():
    db = FakeDB(cron_due=[{"id": 5, "task": "check calendar for conflicts"}])
    alfred = FakeAlfred(db=db)
    hb = CognitiveHeartbeat(alfred)

    alerts = _run(hb._run_due_cron_tasks())

    assert len(alerts) == 1
    assert alerts[0]["type"] == "heartbeat"
    assert alerts[0]["source"] == "cron"
    assert "check calendar for conflicts" in alerts[0]["content"]
    assert alfred.execute_calls == ["check calendar for conflicts"]
    assert db.last_run_ids == [5]


def test_run_due_cron_tasks_records_error_and_still_updates_last_run():
    async def failing_execute(task, context):
        raise RuntimeError("boom")

    db = FakeDB(cron_due=[{"id": 9, "task": "do something broken"}])
    alfred = FakeAlfred(db=db, execute_fn=failing_execute)
    hb = CognitiveHeartbeat(alfred)

    alerts = _run(hb._run_due_cron_tasks())

    assert len(alerts) == 1
    assert alerts[0]["type"] == "heartbeat_error"
    assert alerts[0]["source"] == "cron"
    assert "boom" in alerts[0]["content"]
    # A tool/task failure must not block last_run from advancing -- a
    # permanently-broken cron task should not be retried every 30 minutes
    # forever without ever recording it ran.
    assert db.last_run_ids == [9]


def test_proactive_reasoning_returns_none_for_nothing_reply():
    router = FakeRouter('{"reply": "nothing"}')
    hb = CognitiveHeartbeat(FakeAlfred(memory=FakeMemory(), router=router))

    assert _run(hb._proactive_reasoning([])) is None


def test_proactive_reasoning_returns_none_for_empty_response():
    router = FakeRouter("")
    hb = CognitiveHeartbeat(FakeAlfred(memory=FakeMemory(), router=router))

    assert _run(hb._proactive_reasoning([])) is None


def test_proactive_reasoning_returns_alert_for_a_real_nudge():
    router = FakeRouter('{"reply": "You have not reviewed your budget in 2 weeks."}')
    hb = CognitiveHeartbeat(FakeAlfred(memory=FakeMemory(), router=router))

    alert = _run(hb._proactive_reasoning([]))

    assert alert is not None
    assert alert["type"] == "heartbeat"
    assert alert["source"] == "reasoning"
    assert "budget" in alert["content"]


def test_proactive_reasoning_with_no_router_returns_none():
    hb = CognitiveHeartbeat(FakeAlfred(memory=FakeMemory(), router=None))
    assert _run(hb._proactive_reasoning([])) is None


def test_tick_aggregates_all_three_steps_and_queues_alerts():
    db = FakeDB(
        reminders=[{"id": 1, "text": "call mom"}],
        cron_due=[{"id": 5, "task": "daily briefing"}],
    )
    router = FakeRouter('{"reply": "Your inbox has 12 unread messages."}')
    alfred = FakeAlfred(db=db, memory=FakeMemory(), router=router)
    hb = CognitiveHeartbeat(alfred)

    result = _run(hb.tick())

    assert len(result) == 3
    sources = {a["source"] for a in result}
    assert sources == {"reminder", "cron", "reasoning"}
    for alert in result:
        assert "timestamp" in alert

    popped = hb.pop_alerts()
    assert popped == result
    assert hb.pop_alerts() == []  # drained


def test_pop_alerts_clears_the_queue():
    hb = CognitiveHeartbeat(FakeAlfred())
    hb._push_alert({"type": "heartbeat", "source": "reminder", "content": "x"})
    hb._push_alert({"type": "heartbeat", "source": "cron", "content": "y"})

    first = hb.pop_alerts()
    assert len(first) == 2
    assert hb.pop_alerts() == []


def test_start_stop_lifecycle_runs_at_least_one_tick():
    db = FakeDB(reminders=[{"id": 42, "text": "ping"}])
    router = FakeRouter('{"reply": "nothing"}')
    alfred = FakeAlfred(db=db, memory=FakeMemory(), router=router)
    hb = CognitiveHeartbeat(alfred, interval_seconds=0.02)

    hb.start()
    assert hb.is_running
    deadline = time.time() + 3.0
    alerts: list = []
    while time.time() < deadline and not alerts:
        alerts = hb.pop_alerts()
        if not alerts:
            time.sleep(0.02)
    hb.stop()

    assert not hb.is_running
    assert any(a["source"] == "reminder" for a in alerts), \
        "expected the reminder alert from at least one real tick"


def test_start_is_idempotent():
    hb = CognitiveHeartbeat(FakeAlfred(), interval_seconds=5)
    hb.start()
    first_thread = hb._thread
    hb.start()  # must not spawn a second thread
    assert hb._thread is first_thread
    hb.stop()


def test_stop_before_start_is_a_noop():
    hb = CognitiveHeartbeat(FakeAlfred(), interval_seconds=5)
    hb.stop()  # must not raise
    assert not hb.is_running


def test_alfred_wires_up_heartbeat_and_wrapper_methods_exist():
    """Not a behavioral test of the real Alfred() (that needs the real
    vault/DB/LLM clients) -- just confirms the wiring landed: the class
    exposes a `heartbeat` attribute of the right type and the thin wrapper
    methods conversation.py added actually call through to it."""
    a = Alfred.__new__(Alfred)
    a.heartbeat = CognitiveHeartbeat(FakeAlfred(), interval_seconds=5)

    assert not a.heartbeat.is_running
    a.start_heartbeat()
    assert a.heartbeat.is_running

    a.heartbeat._push_alert({"type": "heartbeat", "source": "reminder", "content": "x"})
    drained = a.pop_heartbeat_alerts()
    assert len(drained) == 1
    assert drained[0]["source"] == "reminder"
    assert drained[0]["content"] == "x"
    assert a.pop_heartbeat_alerts() == []  # drained

    a.stop_heartbeat()
    assert not a.heartbeat.is_running


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain functions
# named test_*, run directly, no pytest).
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
