"""
Execution log tests — Phase 3 self-audit-loop prerequisite.

`Alfred.execute()` timings/tool-outcomes used to die with the HTTP response;
nothing durable survived a turn. PR #16 (skill validation) flagged this as
the actual blocker for the self-audit loop: no persistence layer, no
scheduler. This builds the persistence half only -- `LocalDB.log_execution()`
plus `get_recent_executions()`/`get_execution_stats()` for a future
self-audit pass to read from. The scheduling/cadence decision (where a
weekly pass would run) is explicitly left open, same as PR #16 left it.

Two kinds of tests, matching this project's own stated discipline
(`build-system/test_cognitive_heartbeat.py`'s pattern per PROGRESS.md):
  1. `LocalDB` methods against a real temp-file sqlite db -- no mocking the
     thing actually being tested.
  2. `Alfred.execute()` wiring against a fake DB spy -- confirms a turn logs
     itself, and that a missing/broken DB never breaks the turn.

Run directly:

    python build-system/test_execution_log.py
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.local_db import LocalDB  # noqa: E402
from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


# ---------------------------------------------------------------------------
# Part 1: LocalDB.execution_log against a real temp-file sqlite db.
# ---------------------------------------------------------------------------

def _temp_db() -> LocalDB:
    tmp = Path(tempfile.mkdtemp()) / "test_alfred.db"
    return LocalDB(db_path=tmp)


def test_log_execution_round_trips():
    db = _temp_db()
    row_id = db.log_execution(
        session_id="sess-1",
        task="what's the weather",
        timings={"total_ms": 120.5, "llm_call_ms": 100.0, "turns_used": 1},
        tool_results=[{"tool": "weather", "success": True}],
        turns_used=1,
    )
    assert row_id and row_id > 0

    rows = db.get_recent_executions(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["task_summary"] == "what's the weather"
    assert row["timings"]["total_ms"] == 120.5
    assert row["tools"] == [{"tool": "weather", "success": True}]
    assert row["turns_used"] == 1
    assert row["had_tool_error"] is False


def test_log_execution_flags_tool_errors():
    db = _temp_db()
    db.log_execution(
        session_id="sess-1",
        task="send an email",
        timings={"total_ms": 50.0},
        tool_results=[{"tool": "email", "success": False}],
        turns_used=1,
    )
    rows = db.get_recent_executions(limit=10)
    assert rows[0]["had_tool_error"] is True


def test_log_execution_truncates_long_task_text():
    db = _temp_db()
    long_task = "x" * 1000
    db.log_execution(
        session_id="sess-1",
        task=long_task,
        timings={},
        tool_results=[],
        turns_used=1,
    )
    row = db.get_recent_executions(limit=1)[0]
    assert len(row["task_summary"]) == 300


def test_get_recent_executions_orders_oldest_to_newest_and_respects_limit():
    db = _temp_db()
    for i in range(5):
        db.log_execution(
            session_id=f"sess-{i}",
            task=f"task {i}",
            timings={},
            tool_results=[],
            turns_used=1,
        )
    rows = db.get_recent_executions(limit=3)
    assert len(rows) == 3
    # Oldest-to-newest (most recent 3, in chronological order) -- matches
    # get_recent_context()'s convention elsewhere in this file.
    assert [r["session_id"] for r in rows] == ["sess-2", "sess-3", "sess-4"]


def test_execution_stats_empty_db():
    db = _temp_db()
    stats = db.get_execution_stats()
    assert stats == {"count": 0, "error_rate": 0.0, "avg_phase_ms": {}, "tool_calls": {}, "tool_failures": {}}


def test_execution_stats_aggregates_correctly():
    db = _temp_db()
    db.log_execution(
        session_id="s1", task="a",
        timings={"llm_call_ms": 100.0, "total_ms": 150.0},
        tool_results=[{"tool": "calculator", "success": True}],
        turns_used=1,
    )
    db.log_execution(
        session_id="s2", task="b",
        timings={"llm_call_ms": 200.0, "total_ms": 250.0},
        tool_results=[{"tool": "calculator", "success": False}, {"tool": "email", "success": True}],
        turns_used=2,
    )
    stats = db.get_execution_stats()
    assert stats["count"] == 2
    assert stats["error_rate"] == 0.5, "1 of 2 executions had a tool error"
    assert stats["avg_phase_ms"]["llm_call_ms"] == 150.0
    assert stats["avg_phase_ms"]["total_ms"] == 200.0
    assert stats["tool_calls"] == {"calculator": 2, "email": 1}
    assert stats["tool_failures"] == {"calculator": 1}


# ---------------------------------------------------------------------------
# Part 2: Alfred.execute() wiring -- logs each turn, never breaks on a bad DB.
# ---------------------------------------------------------------------------

class LLMResponse:
    def __init__(self, text=None, provider=None, fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


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


class SpyDB:
    """Records every log_execution() call instead of touching real sqlite."""

    def __init__(self):
        self.calls = []

    def log_execution(self, **kwargs):
        self.calls.append(kwargs)


class ExplodingDB:
    """A DB whose log_execution() always raises -- the turn must still succeed."""

    def log_execution(self, **kwargs):
        raise RuntimeError("disk is on fire")


def make_alfred(router_responses, db=None):
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory()
    a.skill_manager = FakeSkillManager()
    a.goal_expander = FakeGoalExpander()
    a.db = db
    a._router = FakeRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    return a


async def _drain_curation(alfred):
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass


async def _test_execute_logs_the_turn():
    spy = SpyDB()
    alfred = make_alfred(['{"reply": "Hello there."}'], db=spy)
    result = await alfred.execute("say hi", {"session_id": "sess-abc"})
    await _drain_curation(alfred)

    assert result["response"] == "Hello there."
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["session_id"] == "sess-abc"
    assert call["task"] == "say hi"
    assert isinstance(call["timings"], dict) and call["timings"]
    assert call["tool_results"] == []
    assert call["turns_used"] == 1


async def _test_execute_logs_tool_results():
    spy = SpyDB()
    alfred = make_alfred(
        ['{"tool": "time", "params": {}}', '{"reply": "It is now that time."}'],
        db=spy,
    )
    await alfred.execute("what time is it", {"session_id": "sess-xyz"})
    await _drain_curation(alfred)

    assert len(spy.calls) == 1
    tool_results = spy.calls[0]["tool_results"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool"] == "time"


async def _test_execute_defaults_session_id_when_missing():
    spy = SpyDB()
    alfred = make_alfred(['{"reply": "ok"}'], db=spy)
    await alfred.execute("no session context given", {})
    await _drain_curation(alfred)

    assert spy.calls[0]["session_id"] == "unknown"


async def _test_execute_survives_a_broken_db():
    """The whole point of the try/except in conversation.py: a logging
    failure must never surface as a broken turn."""
    alfred = make_alfred(['{"reply": "still works"}'], db=ExplodingDB())
    result = await alfred.execute("say hi", {"session_id": "sess-1"})
    await _drain_curation(alfred)

    assert result["response"] == "still works"


async def _test_execute_with_no_db_never_calls_it():
    """db=None is the existing bootstrap/test-fake shape (see
    test_speed_audit_timing.py) -- must remain a no-op, not an AttributeError."""
    alfred = make_alfred(['{"reply": "fine"}'], db=None)
    result = await alfred.execute("say hi", {"session_id": "sess-1"})
    await _drain_curation(alfred)

    assert result["response"] == "fine"


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain functions
# named test_*, auto-collected and run directly, no pytest).
# ---------------------------------------------------------------------------

def test_execute_logs_the_turn():
    asyncio.run(_test_execute_logs_the_turn())


def test_execute_logs_tool_results():
    asyncio.run(_test_execute_logs_tool_results())


def test_execute_defaults_session_id_when_missing():
    asyncio.run(_test_execute_defaults_session_id_when_missing())


def test_execute_survives_a_broken_db():
    asyncio.run(_test_execute_survives_a_broken_db())


def test_execute_with_no_db_never_calls_it():
    asyncio.run(_test_execute_with_no_db_never_calls_it())


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
    print(f"\n{passed}/{len(tests)} execution_log tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
