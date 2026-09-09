"""
LocalDB reminder methods -- relocated heartbeat (ROADMAP.md Phase 3).

Run directly:
    python build-system/test_local_db.py

The `reminders` table has existed in the schema since before this branch,
but every method that reads or writes it (add/list/delete/get_due/
mark_fired) was missing from the v2 rebuild -- a reminder could not be set,
listed, deleted, or ever detected as due. This covers the restored methods
against a real (temp-file) SQLite DB rather than mocking sqlite3 itself,
since the whole point is to catch a wrong SQL string.

Deliberately imports only brain.local_db, not brain_api.server or brain
(the top-level package pulls in faiss/sentence-transformers via
neural_memory.py, not installed in this cloud sandbox).
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.local_db import LocalDB  # noqa: E402


def fresh_db() -> LocalDB:
    tmp = Path(tempfile.mkdtemp()) / "test_alfred.db"
    return LocalDB(db_path=tmp)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_add_reminder_returns_an_id_and_defaults_category():
    db = fresh_db()
    rid = db.add_reminder("call mom", iso(datetime.now()))
    assert isinstance(rid, int) and rid > 0
    rows = db.list_reminders()
    assert rows[0]["category"] == "general"


def test_list_reminders_excludes_fired_by_default():
    db = fresh_db()
    r1 = db.add_reminder("pending", iso(datetime.now()))
    r2 = db.add_reminder("will fire", iso(datetime.now()))
    db.mark_reminder_fired(r2)

    pending_only = db.list_reminders()
    assert [r["id"] for r in pending_only] == [r1]

    everything = db.list_reminders(include_fired=True)
    assert {r["id"] for r in everything} == {r1, r2}
    fired_row = next(r for r in everything if r["id"] == r2)
    assert fired_row["fired"] == 1


def test_list_reminders_orders_by_due_at_ascending():
    db = fresh_db()
    later = iso(datetime.now() + timedelta(hours=2))
    sooner = iso(datetime.now() + timedelta(hours=1))
    db.add_reminder("later one", later)
    db.add_reminder("sooner one", sooner)
    rows = db.list_reminders()
    assert [r["text"] for r in rows] == ["sooner one", "later one"]


def test_delete_reminder_true_on_success_false_when_missing():
    db = fresh_db()
    rid = db.add_reminder("x", iso(datetime.now()))
    assert db.delete_reminder(rid) is True
    assert db.delete_reminder(rid) is False
    assert db.list_reminders() == []


def test_get_due_reminders_only_returns_unfired_past_due():
    db = fresh_db()
    past = db.add_reminder("overdue", iso(datetime.now() - timedelta(minutes=5)))
    future = db.add_reminder("not yet", iso(datetime.now() + timedelta(hours=1)))
    already_fired = db.add_reminder("old news", iso(datetime.now() - timedelta(days=1)))
    db.mark_reminder_fired(already_fired)

    due = db.get_due_reminders()
    assert [r["id"] for r in due] == [past]
    assert future not in [r["id"] for r in due]


def test_mark_reminder_fired_removes_it_from_due_and_default_listing():
    db = fresh_db()
    rid = db.add_reminder("overdue", iso(datetime.now() - timedelta(minutes=1)))
    assert len(db.get_due_reminders()) == 1

    db.mark_reminder_fired(rid)

    assert db.get_due_reminders() == []
    assert db.list_reminders() == []
    assert db.list_reminders(include_fired=True)[0]["fired"] == 1


def test_get_due_reminders_orders_earliest_first():
    db = fresh_db()
    now = datetime.now()
    later_overdue = db.add_reminder("less overdue", iso(now - timedelta(minutes=1)))
    earlier_overdue = db.add_reminder("more overdue", iso(now - timedelta(hours=1)))
    due = db.get_due_reminders()
    assert [r["id"] for r in due] == [earlier_overdue, later_overdue]


def test_list_reminders_and_get_due_reminders_use_the_shared_lock():
    """Confirmed live 2026-09-09: list_reminders()/get_due_reminders() were
    the only two reminder methods that skipped `with self._lock:` -- every
    other method on this connection (including plain reads elsewhere in
    the file) goes through it, the real serialization mechanism for the
    shared check_same_thread=False connection. Same defect class already
    found and fixed in the self-audit log's read methods. A
    single-threaded call can't distinguish "works" from "works but isn't
    actually serialized against a concurrent writer" -- this checks the
    lock is genuinely acquired."""
    db = fresh_db()

    class _TrackingLock:
        def __init__(self, real_lock):
            self._real = real_lock
            self.entered = False

        def __enter__(self):
            self.entered = True
            return self._real.__enter__()

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    tracking = _TrackingLock(db._lock)
    db._lock = tracking

    db.list_reminders()
    assert tracking.entered, "list_reminders() must acquire self._lock"

    tracking.entered = False
    db.get_due_reminders()
    assert tracking.entered, "get_due_reminders() must acquire self._lock"


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
    print(f"\n{passed}/{len(tests)} local_db reminder tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
