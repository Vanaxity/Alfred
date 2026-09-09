"""
Tool Forge unit tests -- Phase 3 (ROADMAP.md: "skill used 3+ times ->
LLM-generated function -> sandboxed validation -> registered tool").

Run directly:
    python build-system/test_tool_forge.py

Covers:
  1. skill_manager.py's usage-tracking gap this closes: success_count now
     survives a from_markdown() round-trip (previously always reset to 0 on
     reload, making the "used 3+ times" threshold unreachable across a
     restart) and mark_skill_used()/should_forge()/mark_skill_forged() wire
     the actual counting.
  2. tool_forge.py's static AST safety check: valid replay code passes;
     imports, forbidden names, dunder attribute access, wrong signature, and
     extra top-level statements are all rejected before anything executes.
  3. tool_forge.py's sandboxed dry run: valid code passes and reports the
     tools it called; code calling a tool outside the skill's own step list
     is rejected; a crash inside the sandboxed code is reported, not raised.
  4. forge_tool_from_skill() end-to-end against a fake LLM router: good code
     from the "LLM" forges successfully; code the LLM wrapped in markdown
     fences is still extracted and forged; bad code from the "LLM" is
     rejected with a reason, not silently swallowed.
  5. make_forged_handler(): the live handler actually calls through to
     ctx["tool_executor"].execute() and returns a proper ToolResult on
     success, on a handler crash, and on a non-dict return value.
  6. Alfred.execute() wiring (brain/v2/conversation.py): a matched-skill
     turn with no step failures calls mark_skill_used(); once should_forge()
     says yes, a forge task gets scheduled without blocking the reply.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.memory.skill_manager import SkillManager, Skill  # noqa: E402
from brain.memory.tool_forge import (  # noqa: E402
    ForgeResult,
    _ast_safety_check,
    _sandbox_dry_run,
    forge_tool_from_skill,
    make_forged_handler,
)
from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402
from brain.v2.tool_executor import ToolResult, create_tool_executor  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GOOD_CODE = (
    'async def run(params, ctx):\n'
    '    r1 = await ctx["tool_executor"].execute("weather", {"location": "auto"}, ctx)\n'
    '    if not r1.success:\n'
    '        return {"success": False, "error": r1.error}\n'
    '    return {"success": True, "output": r1.output}\n'
)


def _weather_skill(skill_id="skill-01", success_count=0, path=""):
    return Skill(
        skill_id=skill_id,
        title="Check the weather",
        description="Look up the current weather.",
        steps=[{"tool": "weather", "description": "get weather", "params": {"location": "auto"}}],
        tags=["weather"],
        complexity="simple",
        success_count=success_count,
        path=path,
    )


def _bare_manager():
    mgr = object.__new__(SkillManager)
    mgr._skills_cache = {}
    return mgr


# ---------------------------------------------------------------------------
# 1. SkillManager usage tracking
# ---------------------------------------------------------------------------

def test_success_count_survives_markdown_round_trip():
    skill = _weather_skill(success_count=2)
    md = skill.to_markdown()
    reloaded = Skill.from_markdown("irrelevant.md", md)
    assert reloaded.success_count == 2, (
        "success_count must round-trip through to_markdown()/from_markdown() -- "
        "previously always reset to 0 on reload"
    )
    assert reloaded.forged_tool_name is None


def test_forged_tool_name_survives_markdown_round_trip():
    skill = _weather_skill(success_count=5)
    skill.forged_tool_name = "skill_skill-01"
    reloaded = Skill.from_markdown("irrelevant.md", skill.to_markdown())
    assert reloaded.forged_tool_name == "skill_skill-01"


def test_mark_skill_used_increments_and_persists():
    fd, path = tempfile.mkstemp(suffix=".md")
    import os
    os.close(fd)
    try:
        mgr = _bare_manager()
        skill = _weather_skill(success_count=1, path=path)
        mgr._skills_cache[skill.skill_id] = skill

        updated = mgr.mark_skill_used(skill.skill_id)
        assert updated.success_count == 2
        assert mgr._skills_cache[skill.skill_id] is skill

        on_disk = Skill.from_markdown(path, Path(path).read_text(encoding="utf-8"))
        assert on_disk.success_count == 2
    finally:
        os.unlink(path)


def test_mark_skill_used_unknown_id_returns_none():
    mgr = _bare_manager()
    assert mgr.mark_skill_used("nope") is None


def test_should_forge_respects_threshold_and_forged_flag():
    mgr = _bare_manager()
    skill = _weather_skill(success_count=2, path="")
    mgr._skills_cache[skill.skill_id] = skill
    assert mgr.should_forge(skill.skill_id) is False

    skill.success_count = 3
    assert mgr.should_forge(skill.skill_id) is True

    skill.forged_tool_name = "skill_skill-01"
    assert mgr.should_forge(skill.skill_id) is False, "an already-forged skill must not be proposed again"


def test_mark_skill_forged_sets_name_and_persists():
    fd, path = tempfile.mkstemp(suffix=".md")
    import os
    os.close(fd)
    try:
        mgr = _bare_manager()
        skill = _weather_skill(success_count=3, path=path)
        mgr._skills_cache[skill.skill_id] = skill

        ok = mgr.mark_skill_forged(skill.skill_id, "skill_skill-01")
        assert ok is True
        assert mgr._skills_cache[skill.skill_id].forged_tool_name == "skill_skill-01"
        assert mgr.should_forge(skill.skill_id) is False
    finally:
        os.unlink(path)


def test_get_skill_returns_cached_skill_or_none():
    mgr = _bare_manager()
    skill = _weather_skill()
    mgr._skills_cache[skill.skill_id] = skill
    assert mgr.get_skill(skill.skill_id) is skill
    assert mgr.get_skill("nope") is None


# ---------------------------------------------------------------------------
# 2. AST safety check
# ---------------------------------------------------------------------------

def test_ast_check_accepts_valid_code():
    assert _ast_safety_check(GOOD_CODE) is None


def test_ast_check_rejects_import():
    code = "import os\nasync def run(params, ctx):\n    return {\"success\": True}\n"
    assert "top-level statement" in _ast_safety_check(code)


def test_ast_check_rejects_forbidden_name():
    code = 'async def run(params, ctx):\n    x = eval("1+1")\n    return {"success": True, "output": x}\n'
    assert "forbidden name" in _ast_safety_check(code)


def test_ast_check_rejects_dunder_attribute_access():
    code = 'async def run(params, ctx):\n    x = ().__class__\n    return {"success": True}\n'
    assert "dunder attribute" in _ast_safety_check(code)


def test_ast_check_rejects_wrong_signature():
    code = 'async def run(params):\n    return {"success": True}\n'
    assert "params, ctx" in _ast_safety_check(code)


def test_ast_check_rejects_non_run_function_name():
    code = 'async def do_it(params, ctx):\n    return {"success": True}\n'
    assert _ast_safety_check(code) is not None


def test_ast_check_rejects_syntax_error():
    assert _ast_safety_check("async def run(params, ctx)\n    pass") is not None


# ---------------------------------------------------------------------------
# 3. Sandboxed dry run
# ---------------------------------------------------------------------------

def test_sandbox_accepts_valid_code():
    skill = _weather_skill()
    assert _sandbox_dry_run(GOOD_CODE, skill) is None


def test_sandbox_rejects_tool_outside_skill_steps():
    code = (
        'async def run(params, ctx):\n'
        '    r1 = await ctx["tool_executor"].execute("shell", {"command": "rm -rf /"}, ctx)\n'
        '    return {"success": r1.success}\n'
    )
    skill = _weather_skill()
    reason = _sandbox_dry_run(code, skill)
    assert reason is not None
    assert "shell" in reason


def test_sandbox_reports_a_crash_without_raising():
    code = 'async def run(params, ctx):\n    return 1 / 0\n'
    skill = _weather_skill()
    reason = _sandbox_dry_run(code, skill)
    assert reason is not None
    assert "raised" in reason


def test_sandbox_rejects_non_dict_return():
    code = 'async def run(params, ctx):\n    return "not a dict"\n'
    skill = _weather_skill()
    reason = _sandbox_dry_run(code, skill)
    assert reason is not None
    assert "dict" in reason


# ---------------------------------------------------------------------------
# 4. forge_tool_from_skill() orchestration
# ---------------------------------------------------------------------------

class _LLMResponse:
    def __init__(self, text, provider="fake", fallback_used=False, fallback_reason=None):
        self.text = text
        self.provider = provider
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason


class _FakeRouter:
    def __init__(self, text, raise_exc=None):
        self._text = text
        self._raise = raise_exc
        self.last_prompt = None

    async def call(self, **kwargs):
        self.last_prompt = kwargs.get("user_message")
        if self._raise:
            raise self._raise
        return _LLMResponse(self._text)


def test_forge_tool_from_skill_succeeds_with_good_code():
    router = _FakeRouter(GOOD_CODE)
    skill = _weather_skill()
    result = run(forge_tool_from_skill(skill, router))
    assert isinstance(result, ForgeResult)
    assert result.ok is True
    assert result.tool_name == f"skill_{skill.skill_id}"
    assert "weather" in router.last_prompt


def test_forge_tool_from_skill_extracts_fenced_code():
    fenced = f"```python\n{GOOD_CODE}```"
    router = _FakeRouter(fenced)
    skill = _weather_skill()
    result = run(forge_tool_from_skill(skill, router))
    assert result.ok is True


def test_forge_tool_from_skill_rejects_bad_code_with_reason():
    router = _FakeRouter("import os\nasync def run(params, ctx):\n    return {}\n")
    skill = _weather_skill()
    result = run(forge_tool_from_skill(skill, router))
    assert result.ok is False
    assert "AST safety check failed" in result.reason


def test_forge_tool_from_skill_handles_router_exception():
    router = _FakeRouter("", raise_exc=RuntimeError("provider down"))
    skill = _weather_skill()
    result = run(forge_tool_from_skill(skill, router))
    assert result.ok is False
    assert "provider down" in result.reason


def test_forge_tool_from_skill_rejects_skill_with_no_steps():
    router = _FakeRouter(GOOD_CODE)
    skill = _weather_skill()
    skill.steps = []
    result = run(forge_tool_from_skill(skill, router))
    assert result.ok is False


# ---------------------------------------------------------------------------
# 5. make_forged_handler()
# ---------------------------------------------------------------------------

class _FakeToolExecutor:
    def __init__(self, result: ToolResult):
        self._result = result
        self.calls = []

    async def execute(self, tool_name, params, ctx=None):
        self.calls.append((tool_name, params))
        return self._result


def test_forged_handler_success_path():
    handler = make_forged_handler(GOOD_CODE, "skill_abc")
    fake_exec = _FakeToolExecutor(ToolResult(success=True, output="72F and sunny"))
    ctx = {"tool_executor": fake_exec}
    result = run(handler({}, ctx))
    assert result.success is True
    assert result.output == "72F and sunny"
    assert result.tool_name == "skill_abc"
    assert fake_exec.calls == [("weather", {"location": "auto"})]


def test_forged_handler_propagates_underlying_failure():
    handler = make_forged_handler(GOOD_CODE, "skill_abc")
    fake_exec = _FakeToolExecutor(ToolResult(success=False, error="weather API down"))
    result = run(handler({}, {"tool_executor": fake_exec}))
    assert result.success is False
    assert result.error == "weather API down"


def test_forged_handler_catches_a_crash():
    code = 'async def run(params, ctx):\n    return 1 / 0\n'
    handler = make_forged_handler(code, "skill_crash")
    result = run(handler({}, {"tool_executor": _FakeToolExecutor(ToolResult(success=True))}))
    assert result.success is False
    assert "crashed" in result.error


def test_forged_handler_rejects_non_dict_return():
    code = 'async def run(params, ctx):\n    return "oops"\n'
    handler = make_forged_handler(code, "skill_baddict")
    result = run(handler({}, {"tool_executor": _FakeToolExecutor(ToolResult(success=True))}))
    assert result.success is False
    assert "expected dict" in result.error


# ---------------------------------------------------------------------------
# 6. Alfred.execute() wiring
# ---------------------------------------------------------------------------

class _FakeMemory:
    def get_context_for_llm(self, query=None):
        return ""

    def t3_find_episodes(self, query, max_results=2):
        return []

    def t3_save_episode(self, title, content):
        return "fake/path.md"


class _FakeExpanded:
    def __init__(self, expanded):
        self.expanded = expanded


class _FakeGoalExpander:
    async def expand(self, user_input):
        return _FakeExpanded(user_input)


class _FakeSkillManagerForForge:
    """Real enough to exercise the execute()-level wiring: find_skill returns
    a fixed skill every time (as if it always matches), and mark_skill_used /
    should_forge / mark_skill_forged / get_skill are recorded so the test can
    assert Alfred actually calls them rather than just not crashing."""

    def __init__(self, skill, forge_after_uses=None):
        self._skill = skill
        self._forge_after_uses = forge_after_uses
        self.mark_used_calls = 0
        self.forged_calls = []

    def find_skill(self, text, search_ecosystem=False):
        return self._skill

    def generate_skill(self, **kwargs):
        return None

    def improve_skill(self, skill_id, note):
        return None

    def mark_skill_used(self, skill_id):
        self.mark_used_calls += 1
        self._skill.success_count += 1
        return self._skill

    def should_forge(self, skill_id):
        if self._forge_after_uses is None:
            return False
        return self._skill.success_count >= self._forge_after_uses

    def get_skill(self, skill_id):
        return self._skill

    def mark_skill_forged(self, skill_id, tool_name):
        self.forged_calls.append((skill_id, tool_name))
        self._skill.forged_tool_name = tool_name
        return True


GOOD_CODE_TIME = (
    'async def run(params, ctx):\n'
    '    r1 = await ctx["tool_executor"].execute("time", {}, ctx)\n'
    '    if not r1.success:\n'
    '        return {"success": False, "error": r1.error}\n'
    '    return {"success": True, "output": r1.output}\n'
)


class _RouterForForgeWiring:
    """First call answers the actual conversation turn; every call after
    that (the fire-and-forget forge attempt, plus any memory-curation call)
    returns valid forged code (for the `time`-based skill used in the wiring
    tests below) so forging succeeds deterministically."""

    def __init__(self, reply_json):
        self._reply_json = reply_json
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            return _LLMResponse(self._reply_json)
        return _LLMResponse(GOOD_CODE_TIME)


def _make_forge_wiring_alfred(skill_manager, router):
    a = Alfred.__new__(Alfred)
    a.memory = _FakeMemory()
    a.skill_manager = skill_manager
    a.goal_expander = _FakeGoalExpander()
    a.db = None
    a._router = router
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    a._mcp_tool_schemas = {}
    a._forged_tool_schemas = {}
    return a


async def _drain(alfred, timeout=3.0):
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=timeout)
        except Exception:
            pass


def _time_skill(skill_id="skill-time-01", success_count=0):
    """Uses the `time` tool rather than `weather`: it's real, synchronous,
    and network-free, so an Alfred.execute() wiring test can let it actually
    run without a live HTTP call to a real weather API."""
    return Skill(
        skill_id=skill_id,
        title="Check the time",
        description="Look up the current time.",
        steps=[{"tool": "time", "description": "get time", "params": {}}],
        tags=["time"],
        complexity="simple",
        success_count=success_count,
        path="",
    )


async def _run_time_turn(alfred):
    return await alfred.execute("what time is it", {})


def test_successful_matched_skill_turn_marks_skill_used():
    skill = _time_skill(success_count=0)
    mgr = _FakeSkillManagerForForge(skill, forge_after_uses=None)
    router = _RouterForForgeWiring('{"tool": "time", "params": {}}')
    alfred = _make_forge_wiring_alfred(mgr, router)

    result = run(_run_time_turn(alfred))
    run(_drain(alfred))

    assert result["tools_called"] == ["time"]
    assert mgr.mark_used_calls == 1, "a clean matched-skill turn must count as one more successful use"
    assert mgr.forged_calls == [], "should_forge() said no -- nothing should be forged yet"


def test_should_forge_true_schedules_forge_and_registers_tool():
    skill = _time_skill(success_count=2)  # one more clean use crosses a threshold of 3
    mgr = _FakeSkillManagerForForge(skill, forge_after_uses=3)
    router = _RouterForForgeWiring('{"tool": "time", "params": {}}')
    alfred = _make_forge_wiring_alfred(mgr, router)

    result = run(_run_time_turn(alfred))
    run(_drain(alfred))

    assert result["tools_called"] == ["time"]
    assert mgr.mark_used_calls == 1
    assert len(mgr.forged_calls) == 1, "crossing the threshold must trigger exactly one forge"
    forged_skill_id, forged_tool_name = mgr.forged_calls[0]
    assert forged_skill_id == skill.skill_id
    assert forged_tool_name in alfred._tool_executor.tool_names, (
        "a successfully forged tool must actually be registered on the live ToolExecutor"
    )
    assert forged_tool_name in alfred._forged_tool_schemas


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
    print(f"\n{passed}/{len(tests)} tool_forge tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
