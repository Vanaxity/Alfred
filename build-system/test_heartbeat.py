"""
brain.heartbeat unit tests -- relocated heartbeat (ROADMAP.md Phase 3).

Run directly:
    python build-system/test_heartbeat.py

Deliberately imports only brain.heartbeat, not brain_api.server or brain
(pulls in faiss/sentence-transformers, not installed in this cloud
sandbox) -- brain/heartbeat.py is kept dependency-free (asyncio + typing
only) specifically so this stays true, same rationale as
test_brain_api_auth.py's for brain_api.auth.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.heartbeat import check_due_reminders, run_heartbeat_loop  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeDB:
    def __init__(self, due):
        self._due = list(due)
        self.fired_ids = []

    def get_due_reminders(self):
        return list(self._due)

    def mark_reminder_fired(self, reminder_id):
        self.fired_ids.append(reminder_id)


def collecting_broadcast():
    sent = []

    async def broadcast(message):
        sent.append(message)

    broadcast.sent = sent
    return broadcast


def test_check_due_reminders_fires_each_due_one():
    db = FakeDB([
        {"id": 1, "text": "call mom", "category": "personal"},
        {"id": 2, "text": "standup", "category": "work"},
    ])
    bc = collecting_broadcast()

    count = run(check_due_reminders(db, bc))

    assert count == 2
    assert db.fired_ids == [1, 2]
    assert bc.sent == [
        {"type": "reminder", "id": 1, "text": "call mom", "category": "personal"},
        {"type": "reminder", "id": 2, "text": "standup", "category": "work"},
    ]


def test_check_due_reminders_defaults_category_when_missing():
    db = FakeDB([{"id": 1, "text": "no category set"}])
    bc = collecting_broadcast()
    run(check_due_reminders(db, bc))
    assert bc.sent[0]["category"] == "general"


def test_check_due_reminders_marks_fired_before_broadcasting():
    # If broadcast raised, a reminder marked fired only *after* a successful
    # broadcast would never get marked -- and would fire again forever on
    # every subsequent tick. Marking first means at worst one broadcast is
    # lost, not an infinite repeat.
    order = []
    db = FakeDB([{"id": 1, "text": "x"}])
    real_mark = db.mark_reminder_fired
    db.mark_reminder_fired = lambda rid: (order.append("marked"), real_mark(rid))

    async def bc(message):
        order.append("broadcast")

    run(check_due_reminders(db, bc))
    assert order == ["marked", "broadcast"]


def test_check_due_reminders_empty_is_a_safe_noop():
    db = FakeDB([])
    bc = collecting_broadcast()
    count = run(check_due_reminders(db, bc))
    assert count == 0
    assert bc.sent == []


def test_run_heartbeat_loop_ticks_repeatedly_and_survives_a_broadcast_error():
    # A broadcast error on one tick (e.g. no WebSocket clients connected)
    # must not kill the loop -- the next tick should still run.
    ticks = {"n": 0}
    db = FakeDB([{"id": 1, "text": "x"}])

    async def flaky_broadcast(message):
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise ConnectionError("no clients")

    async def scenario():
        task = asyncio.create_task(run_heartbeat_loop(db, flaky_broadcast, interval=0))
        for _ in range(50):
            if ticks["n"] >= 2:
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert ticks["n"] >= 2, "loop must keep polling after a tick raises"


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
