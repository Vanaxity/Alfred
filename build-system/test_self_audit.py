"""
Self-Audit Loop tests — ROADMAP.md Phase 3 ("weekly cron feeds Alfred its own
execution logs, proposes one concrete optimization").

Covers three layers:
    1. LocalDB's new execution_log/self_audit_log tables (real sqlite against
       a throwaway temp file — no mocking needed, this is pure local state).
    2. brain/self_audit.py's summarization + LLM-proposal logic (fake router,
       fake db).
    3. The wiring: Alfred.execute() writing one execution_log row per turn,
       and the `self_audit` tool handler exposing run_self_audit() through
       ToolExecutor.

No network, no real LLM keys, no real vault. Run directly:

    python build-system/test_self_audit.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.local_db import LocalDB  # noqa: E402
from brain.self_audit import (  # noqa: E402
    summarize_executions,
    format_summary_for_prompt,
    run_self_audit,
)
from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes shared across layers 2 and 3
# ---------------------------------------------------------------------------

class LLMResponse:
    """Local stand-in for brain.llm_router.LLMResponse's shape -- avoids
    importing llm_router.py itself, which pulls in the groq/openai/
    google-genai SDKs at module level purely for class definitions this
    test never touches."""

    def __init__(self, text=None, provider=None, fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


class FakeRouter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0
        self.last_user_message = None

    async def call(self, **kwargs):
        self.call_count += 1
        self.last_user_message = kwargs.get("user_message")
        text = self._responses.pop(0) if self._responses else '{"reply": "nothing to save"}'
        return LLMResponse(text=text, provider="fake")


class FakeAuditDB:
    """In-memory stand-in for the execution_log/self_audit_log slice of
    LocalDB -- layer 2 tests exercise brain/self_audit.py in isolation from
    real sqlite (layer 1 below already covers the real table)."""

    def __init__(self, execution_rows=None, self_audits=None):
        self._execution_rows = list(execution_rows or [])
        self._self_audits = list(self_audits or [])
        self.logged_audits = []

    def get_recent_executions(self, days=7, limit=1000):
        return list(self._execution_rows)

    def get_recent_self_audits(self, limit=5):
        return list(self._self_audits[:limit])

    def log_self_audit(self, days, summary_json, proposal):
        self.logged_audits.append({"days": days, "summary_json": summary_json, "proposal": proposal})


def _row(tools=None, success_all=True, total_ms=100.0, turns_used=1,
         completion_claim_nudge=False, time_mismatch_nudge=False, max_turns_hit=False,
         tool_error_count=None):
    tools = tools or []
    return {
        "tools_called": json.dumps(tools),
        "tool_error_count": (0 if success_all else 1) if tool_error_count is None else tool_error_count,
        "total_ms": total_ms,
        "turns_used": turns_used,
        "completion_claim_nudge": 1 if completion_claim_nudge else 0,
        "time_mismatch_nudge": 1 if time_mismatch_nudge else 0,
        "max_turns_hit": 1 if max_turns_hit else 0,
    }


# ---------------------------------------------------------------------------
# Layer 1: LocalDB execution_log / self_audit_log tables (real sqlite)
# ---------------------------------------------------------------------------

def _test_local_db_log_execution_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalDB(db_path=Path(tmp) / "test.db")
        row_id = db.log_execution(
            session_id="s1", task_summary="what's the weather",
            turns_used=2, total_ms=1234.5, llm_call_ms=1000.0,
            tool_execution_ms=50.0, tools_called=["weather"],
            tool_error_count=0, completion_claim_nudge=False,
            time_mismatch_nudge=False, awaiting_approval=False,
            max_turns_hit=False,
        )
        assert row_id and row_id > 0

        rows = db.get_recent_executions(days=7)
        assert len(rows) == 1
        r = rows[0]
        assert r["session_id"] == "s1"
        assert r["turns_used"] == 2
        assert json.loads(r["tools_called"]) == ["weather"]
        assert r["tool_error_count"] == 0
        assert r["max_turns_hit"] == 0
        # Windows keeps a file handle open on an unclosed sqlite3 connection,
        # which makes TemporaryDirectory's own cleanup fail with WinError 32
        # right as this `with` block exits (passed in the cloud sandbox's
        # Linux target, where unlinking an open file is allowed -- confirmed
        # live 2026-09-09 running this suite on a real Windows target).
        db.close()


def _test_local_db_get_recent_executions_filters_by_window():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalDB(db_path=Path(tmp) / "test.db")
        conn = db._get_conn()
        # Insert a row backdated 30 days -- outside a 7-day window -- directly,
        # since log_execution() always stamps "now".
        conn.execute(
            """
            INSERT INTO execution_log (session_id, task_summary, created_at)
            VALUES ('old', 'stale row', datetime('now', '-30 days'))
            """
        )
        conn.commit()
        db.log_execution(
            session_id="new", task_summary="fresh row", turns_used=1,
            total_ms=1.0, llm_call_ms=1.0, tool_execution_ms=0.0,
            tools_called=[], tool_error_count=0, completion_claim_nudge=False,
            time_mismatch_nudge=False, awaiting_approval=False, max_turns_hit=False,
        )

        recent = db.get_recent_executions(days=7)
        assert len(recent) == 1, f"expected only the fresh row, got {[r['session_id'] for r in recent]}"
        assert recent[0]["session_id"] == "new"

        everything = db.get_recent_executions(days=60)
        assert len(everything) == 2
        db.close()


def _test_get_recent_executions_and_self_audits_use_the_shared_lock():
    """Confirmed live 2026-09-09: get_recent_executions()/get_recent_self_audits()
    were the only two LocalDB methods that skipped `with self._lock:` --
    every other method, including plain reads, goes through it (the real
    serialization mechanism for the shared check_same_thread=False
    connection, despite this file's own "no locks needed" docstring). A
    single-threaded call can't distinguish "works" from "works but isn't
    actually serialized against a concurrent writer" -- this checks the
    lock is genuinely acquired, not just that the query still returns
    correct rows."""
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalDB(db_path=Path(tmp) / "test.db")

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

        db.get_recent_executions(days=7)
        assert tracking.entered, "get_recent_executions() must acquire self._lock"

        tracking.entered = False
        db.get_recent_self_audits()
        assert tracking.entered, "get_recent_self_audits() must acquire self._lock"

        db.close()


def _test_local_db_self_audit_log_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalDB(db_path=Path(tmp) / "test.db")
        db.log_self_audit(days=7, summary_json='{"turn_count": 3}', proposal="Cache T3 embeddings.")
        recent = db.get_recent_self_audits(limit=5)
        assert len(recent) == 1
        assert recent[0]["proposal"] == "Cache T3 embeddings."
        assert json.loads(recent[0]["summary_json"])["turn_count"] == 3
        db.close()


# ---------------------------------------------------------------------------
# Layer 2: brain/self_audit.py
# ---------------------------------------------------------------------------

def _test_summarize_executions_empty():
    summary = summarize_executions([])
    assert summary == {"turn_count": 0}
    assert format_summary_for_prompt(summary) == "No execution history recorded for this period."


def _test_summarize_executions_aggregates_correctly():
    rows = [
        _row(tools=["weather"], success_all=True, total_ms=100.0),
        _row(tools=["weather", "web_search"], success_all=False, total_ms=300.0),
        _row(tools=["time"], success_all=True, total_ms=200.0, completion_claim_nudge=True),
        _row(tools=[], success_all=True, total_ms=50.0, max_turns_hit=True),
    ]
    summary = summarize_executions(rows)

    assert summary["turn_count"] == 4
    assert summary["avg_total_ms"] == round((100.0 + 300.0 + 200.0 + 50.0) / 4, 1)
    assert summary["error_turn_count"] == 1
    assert summary["error_rate"] == round(1 / 4, 3)
    assert summary["nudge_turn_count"] == 1
    assert summary["max_turns_hit_count"] == 1
    top_tools = dict(summary["top_tools"])
    assert top_tools["weather"] == 2
    assert top_tools["time"] == 1
    assert top_tools["web_search"] == 1

    text = format_summary_for_prompt(summary)
    assert "4 turns reviewed" in text
    assert "weather" in text


def _test_summarize_executions_tolerates_malformed_tools_called():
    rows = [{"tools_called": "not json", "total_ms": 10.0, "turns_used": 1}]
    summary = summarize_executions(rows)
    assert summary["turn_count"] == 1
    assert summary["top_tools"] == []


async def _test_run_self_audit_empty_history_skips_llm_call():
    router = FakeRouter(["should not be used"])
    db = FakeAuditDB(execution_rows=[])

    result = await run_self_audit(db, router, days=7)

    assert router.call_count == 0, "no execution history means nothing to send the LLM"
    assert "nothing to audit yet" in result["proposal"]
    assert len(db.logged_audits) == 1, "even the empty-history case should be logged"


async def _test_run_self_audit_calls_llm_with_summary_and_persists():
    router = FakeRouter(['Cache T3 embeddings to cut avg_total_ms.'])
    db = FakeAuditDB(execution_rows=[
        _row(tools=["weather"], total_ms=100.0),
        _row(tools=["weather"], total_ms=300.0, success_all=False),
    ])

    result = await run_self_audit(db, router, days=7)

    assert router.call_count == 1
    assert result["proposal"] == "Cache T3 embeddings to cut avg_total_ms."
    assert result["summary"]["turn_count"] == 2
    assert "turns reviewed" in router.last_user_message
    assert len(db.logged_audits) == 1
    assert db.logged_audits[0]["proposal"] == result["proposal"]


async def _test_run_self_audit_references_prior_proposal():
    router = FakeRouter(["Same bottleneck persists, still worth fixing."])
    db = FakeAuditDB(
        execution_rows=[_row(tools=["weather"], total_ms=100.0)],
        self_audits=[{"proposal": "Cache T3 embeddings.", "summary_json": "{}"}],
    )

    await run_self_audit(db, router, days=7)

    assert "Cache T3 embeddings." in router.last_user_message, (
        "the prompt should reference what the last self-audit already proposed, "
        "so the model doesn't just repeat itself blind to prior findings"
    )


async def _test_run_self_audit_blank_llm_response_falls_back():
    router = FakeRouter([None])
    db = FakeAuditDB(execution_rows=[_row(tools=["weather"])])

    result = await run_self_audit(db, router, days=7)

    assert result["proposal"] == "Self-audit LLM call returned no content."


# ---------------------------------------------------------------------------
# Layer 3: wiring -- Alfred.execute() logs, and the `self_audit` tool
# ---------------------------------------------------------------------------

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


class RecordingDB(FakeAuditDB):
    """Extends the layer-2 fake with log_execution(), so execute()'s
    best-effort write can be captured and asserted on directly."""

    def __init__(self):
        super().__init__()
        self.logged_executions = []

    def log_execution(self, **kwargs):
        self.logged_executions.append(kwargs)
        return len(self.logged_executions)


def _make_alfred(router_responses, db=None):
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


async def _test_execute_logs_one_row_per_turn():
    db = RecordingDB()
    alfred = _make_alfred(['{"reply": "Hello there."}'], db=db)

    result = await alfred.execute("say hi", {"session_id": "sess-123"})
    await _drain_curation(alfred)

    assert result["response"] == "Hello there."
    assert len(db.logged_executions) == 1
    logged = db.logged_executions[0]
    assert logged["session_id"] == "sess-123"
    assert logged["turns_used"] == 1
    assert logged["tools_called"] == []
    assert logged["tool_error_count"] == 0
    assert logged["completion_claim_nudge"] is False
    assert logged["awaiting_approval"] is False
    assert logged["max_turns_hit"] is False


async def _test_execute_logs_tool_errors():
    db = RecordingDB()
    # calculator with a bad expression fails without needing approval, then
    # the model gives up with a plain reply.
    alfred = _make_alfred([
        '{"tool": "calculator", "params": {"expression": "not math"}}',
        '{"reply": "Could not compute that."}',
    ], db=db)

    result = await alfred.execute("compute nonsense", {})
    await _drain_curation(alfred)

    assert len(db.logged_executions) == 1
    logged = db.logged_executions[0]
    assert logged["tools_called"] == ["calculator"]
    assert logged["tool_error_count"] == 1, "the failed calculator call must be counted as an error"


async def _test_execute_never_raises_when_db_is_none():
    """make_alfred-style tests elsewhere in this suite (e.g.
    test_speed_audit_timing.py) set a.db = None deliberately -- logging must
    stay best-effort and never break the turn itself."""
    alfred = _make_alfred(['{"reply": "fine"}'], db=None)
    result = await alfred.execute("anything", {})
    await _drain_curation(alfred)
    assert result["response"] == "fine"


async def _test_self_audit_tool_returns_proposal():
    db = FakeAuditDB(execution_rows=[_row(tools=["weather"], total_ms=150.0)])
    router = FakeRouter(["Batch T3 lookups to shave latency."])
    executor = create_tool_executor()

    result = await executor.execute("self_audit", {"days": 7}, {"db": db, "router": router})

    assert result.success
    assert result.output == "Batch T3 lookups to shave latency."
    assert result.metadata["summary"]["turn_count"] == 1


async def _test_self_audit_tool_defaults_days_to_seven():
    db = FakeAuditDB(execution_rows=[])
    calls = {}

    class TrackingDB(FakeAuditDB):
        def get_recent_executions(self, days=7, limit=1000):
            calls["days"] = days
            return []

    tdb = TrackingDB()
    router = FakeRouter([])
    executor = create_tool_executor()

    result = await executor.execute("self_audit", {}, {"db": tdb, "router": router})

    assert result.success
    assert calls["days"] == 7


async def _test_self_audit_tool_requires_db_and_router():
    executor = create_tool_executor()
    result = await executor.execute("self_audit", {}, {"db": None, "router": None})
    assert not result.success
    assert "requires db and router" in result.error


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain functions
# named test_*, run directly, no pytest)
# ---------------------------------------------------------------------------

def test_local_db_log_execution_round_trips():
    _test_local_db_log_execution_round_trips()


def test_local_db_get_recent_executions_filters_by_window():
    _test_local_db_get_recent_executions_filters_by_window()


def test_local_db_self_audit_log_round_trips():
    _test_local_db_self_audit_log_round_trips()


def test_get_recent_executions_and_self_audits_use_the_shared_lock():
    _test_get_recent_executions_and_self_audits_use_the_shared_lock()


def test_summarize_executions_empty():
    _test_summarize_executions_empty()


def test_summarize_executions_aggregates_correctly():
    _test_summarize_executions_aggregates_correctly()


def test_summarize_executions_tolerates_malformed_tools_called():
    _test_summarize_executions_tolerates_malformed_tools_called()


def test_run_self_audit_empty_history_skips_llm_call():
    asyncio.run(_test_run_self_audit_empty_history_skips_llm_call())


def test_run_self_audit_calls_llm_with_summary_and_persists():
    asyncio.run(_test_run_self_audit_calls_llm_with_summary_and_persists())


def test_run_self_audit_references_prior_proposal():
    asyncio.run(_test_run_self_audit_references_prior_proposal())


def test_run_self_audit_blank_llm_response_falls_back():
    asyncio.run(_test_run_self_audit_blank_llm_response_falls_back())


def test_execute_logs_one_row_per_turn():
    asyncio.run(_test_execute_logs_one_row_per_turn())


def test_execute_logs_tool_errors():
    asyncio.run(_test_execute_logs_tool_errors())


def test_execute_never_raises_when_db_is_none():
    asyncio.run(_test_execute_never_raises_when_db_is_none())


def test_self_audit_tool_returns_proposal():
    asyncio.run(_test_self_audit_tool_returns_proposal())


def test_self_audit_tool_defaults_days_to_seven():
    asyncio.run(_test_self_audit_tool_defaults_days_to_seven())


def test_self_audit_tool_requires_db_and_router():
    asyncio.run(_test_self_audit_tool_requires_db_and_router())


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
    print(f"\n{passed}/{len(tests)} self_audit tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
