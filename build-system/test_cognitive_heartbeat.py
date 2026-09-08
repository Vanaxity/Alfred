"""
Cognitive heartbeat tests — Phase 3's "relocated heartbeat" (ROADMAP.md).

Covers two things restored/built this run:
  1. brain/local_db.py's get_due_reminders/mark_reminder_fired/
     get_due_scheduled_tasks -- dropped in the 2026-08-23 heartbeat removal,
     restored here against a real (temp-file) sqlite db so the cron-due
     logic is checked against real croniter behavior, not a fake.
  2. brain/v2/heartbeat.py's CognitiveHeartbeat -- the actual "confidence-
     gated push-context" upgrade the manifesto asked for. Uses fakes for
     memory/db/router (no real LLM keys, no real vault, no real Google
     auth -- this is a cloud session, per the project's fail-safe rules).

Run directly:
    python build-system/test_cognitive_heartbeat.py
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.local_db import LocalDB  # noqa: E402
from brain.v2.heartbeat import CognitiveHeartbeat  # noqa: E402


# ---------------------------------------------------------------------------
# local_db.py: reminders / scheduled tasks
# ---------------------------------------------------------------------------

def _temp_db() -> LocalDB:
    tmp = Path(tempfile.mkdtemp()) / "test_alfred.db"
    return LocalDB(db_path=tmp)


def test_get_due_reminders_returns_only_unfired_past_due():
    db = _temp_db()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO reminders (text, due_at, fired) VALUES (?, datetime('now', '-1 hour'), 0)",
        ("past due, unfired",),
    )
    conn.execute(
        "INSERT INTO reminders (text, due_at, fired) VALUES (?, datetime('now', '-1 hour'), 1)",
        ("past due, already fired",),
    )
    conn.execute(
        "INSERT INTO reminders (text, due_at, fired) VALUES (?, datetime('now', '+1 hour'), 0)",
        ("not due yet",),
    )
    conn.commit()

    due = db.get_due_reminders()
    assert len(due) == 1, f"expected exactly 1 due reminder, got {due}"
    assert due[0]["text"] == "past due, unfired"


def test_mark_reminder_fired_removes_it_from_due_list():
    db = _temp_db()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO reminders (text, due_at, fired) VALUES (?, datetime('now', '-1 hour'), 0)",
        ("fire me",),
    )
    conn.commit()

    due = db.get_due_reminders()
    assert len(due) == 1
    db.mark_reminder_fired(due[0]["id"])
    assert db.get_due_reminders() == []


def test_get_due_scheduled_tasks_uses_cron_expr_against_last_run():
    db = _temp_db()
    conn = db._get_conn()
    # Every-minute cron, last run 2 minutes ago -> due now.
    conn.execute(
        "INSERT INTO scheduled_tasks (task, cron_expr, active, last_run) "
        "VALUES (?, ?, 1, datetime('now', '-2 minutes'))",
        ("due task", "* * * * *"),
    )
    # Every-minute cron, last run 5 seconds ago -> not due yet.
    conn.execute(
        "INSERT INTO scheduled_tasks (task, cron_expr, active, last_run) "
        "VALUES (?, ?, 1, datetime('now', '-5 seconds'))",
        ("not due task", "* * * * *"),
    )
    # Inactive task, otherwise due -- must never be returned.
    conn.execute(
        "INSERT INTO scheduled_tasks (task, cron_expr, active, last_run) "
        "VALUES (?, ?, 0, datetime('now', '-1 hour'))",
        ("inactive task", "* * * * *"),
    )
    conn.commit()

    due_tasks = [t["task"] for t in db.get_due_scheduled_tasks()]
    assert due_tasks == ["due task"], f"unexpected due set: {due_tasks}"


def test_get_due_scheduled_tasks_skips_malformed_cron_without_crashing():
    db = _temp_db()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (task, cron_expr, active, last_run) "
        "VALUES (?, ?, 1, datetime('now', '-1 hour'))",
        ("bad cron", "not a cron expression"),
    )
    conn.commit()

    assert db.get_due_scheduled_tasks() == []  # must not raise


def test_update_last_run_still_works():
    db = _temp_db()
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO scheduled_tasks (task, cron_expr, active, last_run) "
        "VALUES (?, ?, 1, datetime('now', '-1 hour'))",
        ("task", "* * * * *"),
    )
    conn.commit()
    task_id = db.get_due_scheduled_tasks()[0]["id"]
    db.update_last_run(task_id)
    # Fresh last_run means the every-minute cron is no longer due.
    assert db.get_due_scheduled_tasks() == []


# ---------------------------------------------------------------------------
# CognitiveHeartbeat: response parsing
# ---------------------------------------------------------------------------

def test_parse_pulse_response_high_confidence():
    raw = "CONFIDENCE: high\n\nSam has a test Monday with no study block scheduled."
    result = CognitiveHeartbeat._parse_pulse_response(raw)
    assert result["confidence"] == "high"
    assert "test Monday" in result["content"]


def test_parse_pulse_response_case_and_whitespace_insensitive():
    raw = "confidence:  MEDIUM  \n\nWorth a nudge."
    result = CognitiveHeartbeat._parse_pulse_response(raw)
    assert result["confidence"] == "medium"
    assert result["content"] == "Worth a nudge."


def test_parse_pulse_response_malformed_defaults_to_low():
    raw = "Everything looks fine, no gaps found."
    result = CognitiveHeartbeat._parse_pulse_response(raw)
    assert result["confidence"] == "low", "an unparseable reply must degrade to low, never a confident nudge"
    assert result["content"] == raw


# ---------------------------------------------------------------------------
# CognitiveHeartbeat: pulse() behavior, with fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeRouter:
    def __init__(self, text):
        self._text = text
        self.call_count = 0
        self.last_kwargs = None

    async def call(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        return FakeResponse(self._text)


class FakeMemory:
    def __init__(self, profile=None, episodes=None):
        self._profile = profile or {}
        self._episodes = episodes or []

    def t4_load_profile(self):
        return self._profile

    def t3_find_episodes(self, query, max_results=5):
        return self._episodes

    def t1_clear_expired(self):
        return 0


class FakeDB:
    def __init__(self, due_reminders=None, due_tasks=None):
        self._due_reminders = due_reminders or []
        self._due_tasks = due_tasks or []
        self.fired_ids = []
        self.updated_task_ids = []

    def get_due_reminders(self):
        return self._due_reminders

    def mark_reminder_fired(self, reminder_id):
        self.fired_ids.append(reminder_id)

    def get_due_scheduled_tasks(self):
        return self._due_tasks

    def update_last_run(self, task_id):
        self.updated_task_ids.append(task_id)


class FakeAlfred:
    def __init__(self, router_text=None, profile=None, episodes=None, execute_result=None):
        self.memory = FakeMemory(profile=profile, episodes=episodes)
        self.db = FakeDB()
        self._router = FakeRouter(router_text or "CONFIDENCE: low\n\nnothing")
        self._execute_result = execute_result or {"response": "done"}

    async def execute(self, task, context=None):
        return self._execute_result


def _mid_afternoon_heartbeat(**kwargs) -> CognitiveHeartbeat:
    """A heartbeat whose pulse() won't be skipped by the quiet-hours guard."""
    alfred = FakeAlfred(**kwargs)
    hb = CognitiveHeartbeat(alfred)
    return hb


async def _test_pulse_skips_llm_call_with_nothing_to_reason_about():
    hb = _mid_afternoon_heartbeat(profile={}, episodes=[])
    # No GWSClient in this sandbox -- gathering calendar/email will raise
    # and be swallowed, same as a real cloud run with no Google auth.
    result = await hb.pulse()
    assert result is None
    assert hb.alfred._router.call_count == 0, "must not spend an LLM call when there's nothing to reason about"


async def _test_pulse_high_confidence_enqueues_and_notifies():
    notified = []

    async def on_alert(alert):
        notified.append(alert)

    hb = _mid_afternoon_heartbeat(
        router_text="CONFIDENCE: high\n\nSam's MIT essay deadline is in 3 days with no drafted section.",
        profile={"Goals": {"mit_admission": "Apply by the deadline"}},
    )
    hb.on_alert = on_alert

    result = await hb.pulse()
    assert result["confidence"] == "high"
    alerts = hb.pop_alerts()
    assert len(alerts) == 1
    assert alerts[0]["type"] == "cognitive_nudge"
    assert alerts[0]["confidence"] == "high"
    assert len(notified) == 1, "on_alert callback must fire for a medium/high confidence result"


async def _test_pulse_low_confidence_does_not_enqueue_or_notify():
    notified = []

    async def on_alert(alert):
        notified.append(alert)

    hb = _mid_afternoon_heartbeat(
        router_text="CONFIDENCE: low\n\nAll goals on track, nothing to add.",
        profile={"Goals": {"mit_admission": "Apply by the deadline"}},
    )
    hb.on_alert = on_alert

    result = await hb.pulse()
    assert result["confidence"] == "low"
    assert hb.pop_alerts() == [], "low confidence must never surface as an alert"
    assert notified == []


async def _test_pulse_respects_quiet_hours():
    import brain.v2.heartbeat as heartbeat_module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 9, 8, 3, 0, 0)  # 3am -- outside 7-23

    original = heartbeat_module.datetime
    heartbeat_module.datetime = FrozenDatetime
    try:
        hb = _mid_afternoon_heartbeat(
            router_text="CONFIDENCE: high\n\nshould never be reached",
            profile={"Goals": {"x": "y"}},
        )
        result = await hb.pulse()
        assert result is None
        assert hb.alfred._router.call_count == 0
    finally:
        heartbeat_module.datetime = original


async def _test_light_tick_fires_due_reminder_and_marks_it():
    alfred = FakeAlfred()
    alfred.db._due_reminders = [{"id": 7, "text": "call the dentist", "category": "health"}]
    hb = CognitiveHeartbeat(alfred)

    await hb.light_tick()

    assert alfred.db.fired_ids == [7]
    alerts = hb.pop_alerts()
    assert any(a["type"] == "reminder" and a["text"] == "call the dentist" for a in alerts)


async def _test_light_tick_executes_due_scheduled_task():
    alfred = FakeAlfred(execute_result={"response": "checked calendar, all clear"})
    alfred.db._due_tasks = [{"id": 3, "task": "check calendar for conflicts"}]
    hb = CognitiveHeartbeat(alfred)

    await hb.light_tick()

    assert alfred.db.updated_task_ids == [3]
    alerts = hb.pop_alerts()
    assert any(a["type"] == "cron" and "checked calendar" in a["content"] for a in alerts)


def test_pop_alerts_drains_and_clears_the_queue():
    alfred = FakeAlfred()
    hb = CognitiveHeartbeat(alfred)
    hb._enqueue({"type": "reminder", "text": "a"})
    hb._enqueue({"type": "reminder", "text": "b"})

    first = hb.pop_alerts()
    assert len(first) == 2
    assert hb.pop_alerts() == [], "pop_alerts must clear the queue, not just read it"


def test_start_is_a_noop_with_no_running_event_loop():
    alfred = FakeAlfred()
    hb = CognitiveHeartbeat(alfred)
    hb.start()  # called from sync code, no running loop -- must not raise
    assert hb._task is None
    assert hb.enabled is False


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

def test_pulse_skips_llm_call_with_nothing_to_reason_about():
    asyncio.run(_test_pulse_skips_llm_call_with_nothing_to_reason_about())


def test_pulse_high_confidence_enqueues_and_notifies():
    asyncio.run(_test_pulse_high_confidence_enqueues_and_notifies())


def test_pulse_low_confidence_does_not_enqueue_or_notify():
    asyncio.run(_test_pulse_low_confidence_does_not_enqueue_or_notify())


def test_pulse_respects_quiet_hours():
    asyncio.run(_test_pulse_respects_quiet_hours())


def test_light_tick_fires_due_reminder_and_marks_it():
    asyncio.run(_test_light_tick_fires_due_reminder_and_marks_it())


def test_light_tick_executes_due_scheduled_task():
    asyncio.run(_test_light_tick_executes_due_scheduled_task())


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
    print(f"\n{passed}/{len(tests)} cognitive_heartbeat tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
