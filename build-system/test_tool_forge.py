"""
Tool Forge unit tests — ROADMAP.md Week 3 ("Finish Tool Forge: the
markdown-skill -> executable-Python conversion path").

These tests use fakes/tempdirs for everything -- no real LLM, no real
vault, no real subprocess. Run directly:

    python build-system/test_tool_forge.py

Covers:
  1.  is_forge_candidate: threshold, success/failure ratio, already-forged,
      attempts-exhausted gates.
  2.  _extract_code_block: fenced and bare function extraction.
  3.  _static_validate: rejects imports, eval/exec/open, dunder access,
      wrong signature/name, non-async def; accepts a clean function.
  4.  _dynamic_validate (sandboxed smoke test): accepts a function that
      calls the fake executor and returns a dict; rejects one that raises,
      returns a non-dict, or times out.
  5.  generate_tool_code: extracts code from a fake router's response.
  6.  forge_from_skill: end-to-end success (persists file + registry entry,
      marks the skill forged, registers into a real ToolExecutor) and
      end-to-end failure (invalid code increments forge_attempts, nothing
      persisted).
  7.  register_forged_tool + a real ToolExecutor.execute() round trip,
      including the ToolResult<->dict adapter boundary.
  8.  load_persisted_forged_tools: re-registers from disk into a fresh
      executor.
  9.  run_forge_pass: only attempts actual candidates.
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.memory import tool_forge  # noqa: E402
from brain.memory.tool_forge import (  # noqa: E402
    ForgeValidation,
    _dynamic_validate,
    _extract_code_block,
    _static_validate,
    forge_from_skill,
    generate_tool_code,
    is_forge_candidate,
    load_persisted_forged_tools,
    register_forged_tool,
    run_forge_pass,
)
from brain.v2.tool_executor import (  # noqa: E402
    ToolExecutor,
    ToolResult,
    Guardrails,
    _action_signature,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSkill:
    def __init__(
        self,
        skill_id="skill-01",
        title="Check the weather then log it",
        description="Checks weather and saves a note.",
        steps=None,
        success_count=3,
        failure_count=0,
        forged_tool=None,
        forge_attempts=0,
    ):
        self.skill_id = skill_id
        self.title = title
        self.description = description
        self.steps = steps if steps is not None else [
            {"tool": "weather", "description": "check weather", "params": {"location": "auto"}},
        ]
        self.success_count = success_count
        self.failure_count = failure_count
        self.forged_tool = forged_tool
        self.forge_attempts = forge_attempts


class FakeSkillManager:
    """Mirrors the real SkillManager's forge-relevant methods, storing
    state directly on the FakeSkill objects passed in."""

    def __init__(self, skills=None):
        self._skills = {s.skill_id: s for s in (skills or [])}

    def get_all_skills(self):
        return list(self._skills.values())

    def record_forge_attempt(self, skill_id):
        skill = self._skills.get(skill_id)
        if skill:
            skill.forge_attempts += 1
        return skill is not None

    def mark_forged(self, skill_id, tool_name):
        skill = self._skills.get(skill_id)
        if skill:
            skill.forged_tool = tool_name
        return skill is not None


class LLMResponse:
    def __init__(self, text):
        self.text = text


class FakeRouter:
    def __init__(self, text):
        self._text = text
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        return LLMResponse(self._text)


VALID_CODE = '''async def run(params: dict, ctx: dict) -> dict:
    location = params.get("location", "auto")
    result = await ctx["tool_executor"].execute("weather", {"location": location}, ctx["tool_context"])
    return {"success": result["success"], "output": result.get("output", "")}
'''


# ---------------------------------------------------------------------------
# 1. is_forge_candidate
# ---------------------------------------------------------------------------

def test_is_forge_candidate_true_at_exactly_threshold():
    assert is_forge_candidate(FakeSkill(success_count=3, failure_count=0)) is True


def test_is_forge_candidate_false_below_threshold():
    assert is_forge_candidate(FakeSkill(success_count=2, failure_count=0)) is False


def test_is_forge_candidate_false_when_already_forged():
    assert is_forge_candidate(FakeSkill(success_count=5, forged_tool="forged_x")) is False


def test_is_forge_candidate_false_when_attempts_exhausted():
    assert is_forge_candidate(FakeSkill(success_count=5, forge_attempts=3)) is False


def test_is_forge_candidate_false_when_failure_ratio_too_high():
    # total=4 clears the threshold, but 2 successes to 2 failures fails
    # the "at least twice as many successes" bar.
    assert is_forge_candidate(FakeSkill(success_count=2, failure_count=2)) is False


def test_is_forge_candidate_true_at_ratio_boundary():
    # 4 successes, 2 failures: success >= 2*failure holds exactly.
    assert is_forge_candidate(FakeSkill(success_count=4, failure_count=2)) is True


# ---------------------------------------------------------------------------
# 2. _extract_code_block
# ---------------------------------------------------------------------------

def test_extract_code_block_from_fenced_python():
    text = f"Here you go:\n```python\n{VALID_CODE}```\nDone."
    extracted = _extract_code_block(text)
    assert extracted is not None
    assert "async def run(" in extracted


def test_extract_code_block_from_bare_fence():
    text = f"```\n{VALID_CODE}```"
    extracted = _extract_code_block(text)
    assert "async def run(" in extracted


def test_extract_code_block_from_unfenced_fallback():
    extracted = _extract_code_block(VALID_CODE)
    assert extracted is not None
    assert "async def run(" in extracted


def test_extract_code_block_returns_none_for_junk():
    assert _extract_code_block("I can't do that.") is None
    assert _extract_code_block("") is None


# ---------------------------------------------------------------------------
# 3. _static_validate
# ---------------------------------------------------------------------------

def test_static_validate_accepts_clean_function():
    result = _static_validate(VALID_CODE)
    assert result.ok is True, result.reason


def test_static_validate_rejects_import():
    code = "import os\nasync def run(params: dict, ctx: dict) -> dict:\n    return {\"success\": True}\n"
    assert _static_validate(code).ok is False


def test_static_validate_rejects_eval():
    code = (
        "async def run(params: dict, ctx: dict) -> dict:\n"
        "    eval('1+1')\n"
        "    return {\"success\": True}\n"
    )
    assert _static_validate(code).ok is False


def test_static_validate_rejects_open():
    code = (
        "async def run(params: dict, ctx: dict) -> dict:\n"
        "    open('/etc/passwd')\n"
        "    return {\"success\": True}\n"
    )
    assert _static_validate(code).ok is False


def test_static_validate_rejects_dunder_access():
    code = (
        "async def run(params: dict, ctx: dict) -> dict:\n"
        "    x = params.__class__\n"
        "    return {\"success\": True}\n"
    )
    assert _static_validate(code).ok is False


def test_static_validate_rejects_wrong_function_name():
    code = "async def do_it(params: dict, ctx: dict) -> dict:\n    return {\"success\": True}\n"
    assert _static_validate(code).ok is False


def test_static_validate_rejects_non_async_def():
    code = "def run(params: dict, ctx: dict) -> dict:\n    return {\"success\": True}\n"
    assert _static_validate(code).ok is False


def test_static_validate_rejects_wrong_signature():
    code = "async def run(a: dict, b: dict, c: dict) -> dict:\n    return {\"success\": True}\n"
    assert _static_validate(code).ok is False


def test_static_validate_rejects_syntax_error():
    assert _static_validate("async def run(:\n    pass").ok is False


# ---------------------------------------------------------------------------
# 4. _dynamic_validate
# ---------------------------------------------------------------------------

async def _test_dynamic_validate_accepts_valid_function():
    skill = FakeSkill()
    result = await _dynamic_validate(VALID_CODE, skill)
    assert result.ok is True, result.reason


async def _test_dynamic_validate_rejects_raising_function():
    code = (
        "async def run(params: dict, ctx: dict) -> dict:\n"
        "    raise RuntimeError('boom')\n"
    )
    result = await _dynamic_validate(code, FakeSkill())
    assert result.ok is False
    assert "boom" in result.reason


async def _test_dynamic_validate_rejects_non_dict_return():
    code = "async def run(params: dict, ctx: dict) -> dict:\n    return 'not a dict'\n"
    result = await _dynamic_validate(code, FakeSkill())
    assert result.ok is False


async def _test_dynamic_validate_rejects_timeout():
    # A candidate function can never legitimately hang on its own (no
    # imports means no real sleep/I/O it could reach) -- the realistic way
    # validation ever times out is a slow ctx["tool_executor"].execute()
    # call underneath it, which _fake_executor_delay simulates.
    original_timeout = tool_forge.VALIDATION_TIMEOUT_SECONDS
    tool_forge.VALIDATION_TIMEOUT_SECONDS = 0.05
    try:
        result = await _dynamic_validate(VALID_CODE, FakeSkill(), _fake_executor_delay=1.0)
    finally:
        tool_forge.VALIDATION_TIMEOUT_SECONDS = original_timeout
    assert result.ok is False
    assert "timed out" in result.reason


# ---------------------------------------------------------------------------
# 5. generate_tool_code
# ---------------------------------------------------------------------------

async def _test_generate_tool_code_extracts_from_router_response():
    router = FakeRouter(f"```python\n{VALID_CODE}```")
    skill = FakeSkill()
    code = await generate_tool_code(skill, router)
    assert code is not None
    assert "async def run(" in code
    assert router.call_count == 1


async def _test_generate_tool_code_returns_none_on_junk_response():
    router = FakeRouter("Sorry, I can't help with that.")
    code = await generate_tool_code(FakeSkill(), router)
    assert code is None


async def _test_generate_tool_code_returns_none_on_router_exception():
    class ExplodingRouter:
        async def call(self, **kwargs):
            raise RuntimeError("provider down")

    code = await generate_tool_code(FakeSkill(), ExplodingRouter())
    assert code is None


# ---------------------------------------------------------------------------
# 6/7/8/9: end-to-end, all against a temp forge directory
# ---------------------------------------------------------------------------

class _TempForgeDir:
    """Points tool_forge's on-disk paths at a scratch directory for the
    duration of a test, restoring the real paths after (these are module
    globals referenced fresh on every call, so reassigning the module
    attributes is enough -- no need to patch every function)."""

    def __enter__(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="alfred_tool_forge_test_"))
        self._orig_dir = tool_forge.T2_FORGED_TOOLS_DIR
        self._orig_registry = tool_forge.REGISTRY_PATH
        tool_forge.T2_FORGED_TOOLS_DIR = self._tmp
        tool_forge.REGISTRY_PATH = self._tmp / "registry.json"
        return self._tmp

    def __exit__(self, *exc):
        tool_forge.T2_FORGED_TOOLS_DIR = self._orig_dir
        tool_forge.REGISTRY_PATH = self._orig_registry
        shutil.rmtree(self._tmp, ignore_errors=True)


async def _test_forge_from_skill_success_persists_and_registers():
    with _TempForgeDir() as tmp:
        skill = FakeSkill()
        skill_manager = FakeSkillManager([skill])
        router = FakeRouter(f"```python\n{VALID_CODE}```")
        executor = ToolExecutor()

        result = await forge_from_skill(skill, skill_manager, router, executor)

        assert result.success is True, result.reason
        assert result.tool_name in executor.tool_names
        assert skill.forged_tool == result.tool_name

        registry = json.loads((tmp / "registry.json").read_text(encoding="utf-8"))
        assert result.tool_name in registry
        assert (tmp / f"{result.tool_name}.py").exists()


async def _test_forge_from_skill_invalid_code_records_attempt_and_persists_nothing():
    with _TempForgeDir() as tmp:
        skill = FakeSkill()
        skill_manager = FakeSkillManager([skill])
        router = FakeRouter("```python\nimport os\nasync def run(params, ctx):\n    return {}\n```")
        executor = ToolExecutor()

        result = await forge_from_skill(skill, skill_manager, router, executor)

        assert result.success is False
        assert skill.forge_attempts == 1
        assert skill.forged_tool is None
        assert not (tmp / "registry.json").exists()


async def _test_forge_from_skill_no_code_from_router_records_attempt():
    with _TempForgeDir():
        skill = FakeSkill()
        skill_manager = FakeSkillManager([skill])
        router = FakeRouter("no code here")
        executor = ToolExecutor()

        result = await forge_from_skill(skill, skill_manager, router, executor)
        assert result.success is False
        assert skill.forge_attempts == 1


async def _test_register_forged_tool_round_trips_through_real_executor():
    """The dict<->ToolResult adapter boundary: a forged tool's `run` talks
    dicts, but it drives a real ToolExecutor.execute() call underneath and
    the outer handler must hand back a real ToolResult."""
    executor = ToolExecutor()

    async def fake_weather_handler(params, ctx):
        return ToolResult(success=True, output=f"Sunny in {params.get('location')}")

    executor.register("weather", fake_weather_handler)

    ok = register_forged_tool(executor, "forged_weather_logger", VALID_CODE)
    assert ok is True
    assert executor._guardrails.get("forged_weather_logger") == Guardrails(require_approval=True)

    approved = {_action_signature("forged_weather_logger", {"location": "Paris"})}
    tool_ctx = {"tool_executor": executor, "approved_actions": approved}
    result = await executor.execute("forged_weather_logger", {"location": "Paris"}, tool_ctx)
    assert isinstance(result, ToolResult)
    assert result.success is True, result.error
    assert "Paris" in result.output


async def _test_load_persisted_forged_tools_reregisters_from_disk():
    with _TempForgeDir():
        skill = FakeSkill()
        skill_manager = FakeSkillManager([skill])
        router = FakeRouter(f"```python\n{VALID_CODE}```")
        executor_a = ToolExecutor()
        result = await forge_from_skill(skill, skill_manager, router, executor_a)
        assert result.success is True

        # Fresh executor, e.g. simulating a process restart.
        executor_b = ToolExecutor()
        assert result.tool_name not in executor_b.tool_names
        schemas = load_persisted_forged_tools(executor_b)

        assert result.tool_name in executor_b.tool_names
        assert result.tool_name in schemas
        assert "description" in schemas[result.tool_name]


async def _test_load_persisted_forged_tools_skips_missing_code_file():
    with _TempForgeDir() as tmp:
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "registry.json").write_text(json.dumps({
            "forged_ghost": {
                "skill_id": "s1", "title": "Ghost", "description": "gone",
                "code_path": str(tmp / "forged_ghost.py"),
                "created_at": "now",
            }
        }), encoding="utf-8")
        executor = ToolExecutor()
        schemas = load_persisted_forged_tools(executor)
        assert schemas == {}
        assert "forged_ghost" not in executor.tool_names


async def _test_run_forge_pass_only_attempts_candidates():
    with _TempForgeDir():
        eligible = FakeSkill(skill_id="eligible", success_count=3, failure_count=0)
        not_yet = FakeSkill(skill_id="not-yet", success_count=1, failure_count=0)
        already_forged = FakeSkill(skill_id="done", success_count=10, forged_tool="forged_done")
        skill_manager = FakeSkillManager([eligible, not_yet, already_forged])
        router = FakeRouter(f"```python\n{VALID_CODE}```")
        executor = ToolExecutor()

        new_schemas = await run_forge_pass(skill_manager, executor, router)

        assert router.call_count == 1, "only the one real candidate should trigger codegen"
        assert len(new_schemas) == 1
        assert eligible.forged_tool is not None
        assert not_yet.forged_tool is None
        assert not_yet.forge_attempts == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_dynamic_validate_accepts_valid_function():
    asyncio.run(_test_dynamic_validate_accepts_valid_function())


def test_dynamic_validate_rejects_raising_function():
    asyncio.run(_test_dynamic_validate_rejects_raising_function())


def test_dynamic_validate_rejects_non_dict_return():
    asyncio.run(_test_dynamic_validate_rejects_non_dict_return())


def test_dynamic_validate_rejects_timeout():
    asyncio.run(_test_dynamic_validate_rejects_timeout())


def test_generate_tool_code_extracts_from_router_response():
    asyncio.run(_test_generate_tool_code_extracts_from_router_response())


def test_generate_tool_code_returns_none_on_junk_response():
    asyncio.run(_test_generate_tool_code_returns_none_on_junk_response())


def test_generate_tool_code_returns_none_on_router_exception():
    asyncio.run(_test_generate_tool_code_returns_none_on_router_exception())


def test_forge_from_skill_success_persists_and_registers():
    asyncio.run(_test_forge_from_skill_success_persists_and_registers())


def test_forge_from_skill_invalid_code_records_attempt_and_persists_nothing():
    asyncio.run(_test_forge_from_skill_invalid_code_records_attempt_and_persists_nothing())


def test_forge_from_skill_no_code_from_router_records_attempt():
    asyncio.run(_test_forge_from_skill_no_code_from_router_records_attempt())


def test_register_forged_tool_round_trips_through_real_executor():
    asyncio.run(_test_register_forged_tool_round_trips_through_real_executor())


def test_load_persisted_forged_tools_reregisters_from_disk():
    asyncio.run(_test_load_persisted_forged_tools_reregisters_from_disk())


def test_load_persisted_forged_tools_skips_missing_code_file():
    asyncio.run(_test_load_persisted_forged_tools_skips_missing_code_file())


def test_run_forge_pass_only_attempts_candidates():
    asyncio.run(_test_run_forge_pass_only_attempts_candidates())


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
