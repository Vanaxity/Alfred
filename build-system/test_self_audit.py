"""
Self-audit loop tests -- Day 7 (Phase 3).

Covers brain/self_audit.py (the execution-log writer/reader/aggregator and
the LLM-driven optimization proposal) plus its integration point in
Alfred.execute() (brain/v2/conversation.py) -- confirming a real turn logs
a record, and a fake-built test instance (Alfred.__new__ bypass, no
_self_audit_log_path set) logs nothing.

No network, no disk outside a per-test tmp directory, no real LLM calls --
every router is a hand-rolled fake. Run directly:

    python build-system/test_self_audit.py
"""

import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.self_audit import (  # noqa: E402
    aggregate_stats,
    log_turn_execution,
    propose_optimization,
    read_execution_log,
    run_self_audit,
)
from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import create_tool_executor  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class LLMResponse:
    """Local stand-in for llm_router.LLMResponse's shape -- avoids importing
    llm_router.py itself, which pulls in the groq/openai/google-genai SDKs
    at module level purely for class definitions this test never touches."""

    def __init__(self, text=None, provider=None, fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


class FakeRouter:
    def __init__(self, text="Batch the two sequential LLM calls."):
        self._text = text
        self.call_count = 0
        self.last_call_kwargs = None

    async def call(self, **kwargs):
        self.call_count += 1
        self.last_call_kwargs = kwargs
        return LLMResponse(text=self._text, provider="fake")


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


class QueuedRouter:
    """Returns queued responses in order; repeats the last one once
    exhausted (fire-and-forget memory curation makes its own extra call)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        text = self._responses.pop(0) if self._responses else '{"reply": "nothing to save"}'
        return LLMResponse(text=text, provider="fake")


def make_alfred(router_responses, self_audit_log_path=None):
    """Alfred instance with every heavy singleton swapped for a fake, built
    without calling Alfred.__init__ (mirrors test_speed_audit_timing.py)."""
    a = Alfred.__new__(Alfred)
    a.memory = FakeMemory()
    a.skill_manager = FakeSkillManager()
    a.goal_expander = FakeGoalExpander()
    a.db = None
    a._router = QueuedRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    if self_audit_log_path is not None:
        a._self_audit_log_path = self_audit_log_path
    return a


async def _drain_curation(alfred):
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass


class TmpDir:
    def __enter__(self):
        self.path = Path(tempfile.mkdtemp(prefix="alfred_self_audit_test_"))
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def _sample_timings(total=100.0, llm=80.0, tool=10.0):
    return {
        "goal_expansion_ms": 5.0,
        "skill_matching_ms": 1.0,
        "memory_snippets_wait_ms": 0.0,
        "pre_loop_total_ms": 6.0,
        "prompt_build_ms": 2.0,
        "llm_call_ms": llm,
        "tool_execution_ms": tool,
        "total_ms": total,
        "turns_used": 2,
    }


# ---------------------------------------------------------------------------
# log_turn_execution / read_execution_log
# ---------------------------------------------------------------------------

async def _test_log_and_read_round_trip():
    with TmpDir() as tmp:
        log_path = tmp / "execution_log.jsonl"
        log_turn_execution(
            "check the weather",
            ["weather"],
            [{"tool": "weather", "output": "sunny", "success": True, "params": {}}],
            _sample_timings(),
            log_path=log_path,
        )
        log_turn_execution(
            "email Sam",
            ["email"],
            [{"tool": "email", "output": "rate limit exceeded", "success": False, "params": {}}],
            _sample_timings(total=50.0, llm=40.0, tool=5.0),
            log_path=log_path,
        )

        records = read_execution_log(log_path=log_path)
        assert len(records) == 2
        assert records[0]["task_preview"] == "check the weather"
        assert records[0]["tools_called"] == ["weather"]
        assert records[0]["tool_failures"] == []
        assert records[1]["tool_failures"] == [{"tool": "email", "detail": "rate limit exceeded"}]
        assert records[1]["timings"]["total_ms"] == 50.0
        assert records[1]["turns_used"] == 2


def test_log_and_read_round_trip():
    asyncio.run(_test_log_and_read_round_trip())


def test_read_execution_log_missing_file_returns_empty():
    with TmpDir() as tmp:
        records = read_execution_log(log_path=tmp / "does-not-exist.jsonl")
        assert records == []


def test_read_execution_log_skips_corrupt_lines():
    with TmpDir() as tmp:
        log_path = tmp / "execution_log.jsonl"
        good = json.dumps({"timestamp": datetime.now().isoformat(), "tools_called": ["time"]})
        log_path.write_text(f"{{not valid json\n{good}\n\n", encoding="utf-8")

        records = read_execution_log(log_path=log_path)
        assert len(records) == 1
        assert records[0]["tools_called"] == ["time"]


def test_read_execution_log_since_filters_old_records():
    with TmpDir() as tmp:
        log_path = tmp / "execution_log.jsonl"
        now = datetime.now()
        old = json.dumps({"timestamp": (now - timedelta(days=10)).isoformat(), "tools_called": ["time"]})
        recent = json.dumps({"timestamp": (now - timedelta(hours=1)).isoformat(), "tools_called": ["weather"]})
        log_path.write_text(f"{old}\n{recent}\n", encoding="utf-8")

        records = read_execution_log(log_path=log_path, since=now - timedelta(days=7))
        assert len(records) == 1
        assert records[0]["tools_called"] == ["weather"]


# ---------------------------------------------------------------------------
# aggregate_stats
# ---------------------------------------------------------------------------

def test_aggregate_stats_empty():
    assert aggregate_stats([]) == {"turn_count": 0}


def test_aggregate_stats_counts_tools_failures_and_timings():
    records = [
        {
            "timestamp": "2026-09-01T10:00:00",
            "tools_called": ["weather"],
            "tool_failures": [],
            "timings": {"llm_call_ms": 100.0, "tool_execution_ms": 10.0, "total_ms": 120.0},
            "turns_used": 1,
        },
        {
            "timestamp": "2026-09-02T10:00:00",
            "tools_called": ["email", "email"],
            "tool_failures": [{"tool": "email", "detail": "rate limit"}],
            "timings": {"llm_call_ms": 300.0, "tool_execution_ms": 20.0, "total_ms": 340.0},
            "turns_used": 3,
        },
    ]

    stats = aggregate_stats(records)
    assert stats["turn_count"] == 2
    assert stats["tool_calls"] == {"weather": 1, "email": 2}
    assert stats["tool_failures"] == {"email": 1}
    assert stats["avg_timings_ms"]["llm_call_ms"] == 200.0
    assert stats["avg_timings_ms"]["tool_execution_ms"] == 15.0
    # llm_call_ms (avg 200) dominates tool_execution_ms (avg 15); total_ms
    # itself must never win since it's an aggregate, not an actual phase.
    assert stats["slowest_phase"] == "llm_call_ms"
    assert stats["avg_turns_per_task"] == 2.0
    assert stats["date_range"] == {"earliest": "2026-09-01T10:00:00", "latest": "2026-09-02T10:00:00"}


# ---------------------------------------------------------------------------
# propose_optimization
# ---------------------------------------------------------------------------

async def _test_propose_optimization_no_data_skips_llm_call():
    router = FakeRouter()
    result = await propose_optimization({"turn_count": 0}, router)
    assert result["based_on_turns"] == 0
    assert "not enough" in result["proposal"].lower()
    assert router.call_count == 0


def test_propose_optimization_no_data_skips_llm_call():
    asyncio.run(_test_propose_optimization_no_data_skips_llm_call())


async def _test_propose_optimization_no_router_configured():
    stats = {"turn_count": 5, "avg_timings_ms": {}, "tool_calls": {}}
    result = await propose_optimization(stats, router=None)
    assert result["proposal"] is None
    assert result["based_on_turns"] == 5
    assert "error" in result


def test_propose_optimization_no_router_configured():
    asyncio.run(_test_propose_optimization_no_router_configured())


async def _test_propose_optimization_calls_router_with_stats():
    router = FakeRouter(text="Cache goal expansion results across similar tasks.")
    stats = {"turn_count": 12, "avg_timings_ms": {"llm_call_ms": 500.0}, "tool_calls": {"email": 4}}

    result = await propose_optimization(stats, router)

    assert router.call_count == 1
    assert result["proposal"] == "Cache goal expansion results across similar tasks."
    assert result["based_on_turns"] == 12
    # the stats actually reached the prompt, not just a call being made
    assert "500.0" in router.last_call_kwargs["user_message"]
    assert "email" in router.last_call_kwargs["user_message"]


def test_propose_optimization_calls_router_with_stats():
    asyncio.run(_test_propose_optimization_calls_router_with_stats())


# ---------------------------------------------------------------------------
# run_self_audit (end-to-end over a real tmp log file)
# ---------------------------------------------------------------------------

async def _test_run_self_audit_end_to_end_respects_lookback_window():
    with TmpDir() as tmp:
        log_path = tmp / "execution_log.jsonl"
        now = datetime.now()
        old = json.dumps({
            "timestamp": (now - timedelta(days=30)).isoformat(),
            "tools_called": ["shell"], "tool_failures": [],
            "timings": {"total_ms": 999.0}, "turns_used": 1,
        })
        recent = json.dumps({
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "tools_called": ["weather"], "tool_failures": [],
            "timings": {"total_ms": 80.0, "llm_call_ms": 60.0}, "turns_used": 1,
        })
        log_path.write_text(f"{old}\n{recent}\n", encoding="utf-8")

        router = FakeRouter(text="One concrete optimization.")
        result = await run_self_audit(lookback_days=7, log_path=log_path, router=router)

        assert result["stats"]["turn_count"] == 1, "the 30-day-old record must fall outside a 7-day window"
        assert result["stats"]["tool_calls"] == {"weather": 1}
        assert result["proposal"] == "One concrete optimization."
        assert result["based_on_turns"] == 1


def test_run_self_audit_end_to_end_respects_lookback_window():
    asyncio.run(_test_run_self_audit_end_to_end_respects_lookback_window())


# ---------------------------------------------------------------------------
# Integration: Alfred.execute() actually writes (or skips) a record
# ---------------------------------------------------------------------------

async def _test_execute_logs_a_turn_when_log_path_is_set():
    with TmpDir() as tmp:
        log_path = tmp / "execution_log.jsonl"
        alfred = make_alfred(['{"reply": "Hello there."}'], self_audit_log_path=log_path)

        result = await alfred.execute("say hi", {})
        await _drain_curation(alfred)

        assert result["response"] == "Hello there."
        records = read_execution_log(log_path=log_path)
        assert len(records) == 1
        assert records[0]["task_preview"] == "say hi"
        assert records[0]["timings"]["total_ms"] == result["timings"]["total_ms"]


def test_execute_logs_a_turn_when_log_path_is_set():
    asyncio.run(_test_execute_logs_a_turn_when_log_path_is_set())


async def _test_execute_skips_logging_when_no_log_path_attribute():
    """The Alfred.__new__ bypass every other test file in build-system/
    already uses must never touch the real repo-relative default path --
    confirms the getattr(..., None) default in conversation.py actually
    disables logging rather than falling back to EXECUTION_LOG_PATH."""
    alfred = make_alfred(['{"reply": "Hello there."}'])
    assert not hasattr(alfred, "_self_audit_log_path")

    result = await alfred.execute("say hi", {})
    await _drain_curation(alfred)

    assert result["response"] == "Hello there."
    from brain.self_audit import EXECUTION_LOG_PATH
    assert not EXECUTION_LOG_PATH.exists(), (
        "a fake test instance with no _self_audit_log_path must not write "
        "to the real default execution log"
    )


def test_execute_skips_logging_when_no_log_path_attribute():
    asyncio.run(_test_execute_skips_logging_when_no_log_path_attribute())


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
    print(f"\n{passed}/{len(tests)} self_audit tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
