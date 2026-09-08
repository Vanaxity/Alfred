"""
Tool Forge tests — Phase 3's "skill used successfully more than 3 times ->
LLM-generated Python function -> sandboxed validation -> registered tool"
pipeline (brain/tool_forge.py), plus SkillManager.record_skill_use(), the
usage-tracking half it depends on.

No real LLM calls: a fake router stands in for brain.llm_router.LLMRouter.
The sandbox tests DO spawn real `python` subprocesses (validate_in_sandbox
itself is pure local subprocess execution, no network/credentials needed),
which is the only way to actually prove the invariant checker + sandbox
reject what they're supposed to reject.

Run directly:
    python build-system/test_tool_forge.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.tool_forge import (  # noqa: E402
    ToolForge,
    check_invariants,
    extract_code,
    validate_in_sandbox,
)
from brain.memory.skill_manager import SkillManager, Skill  # noqa: E402
from brain.v2.tool_executor import ToolExecutor, Guardrails, _action_signature  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeLLMResponse:
    def __init__(self, text):
        self.text = text


class FakeRouter:
    def __init__(self, text=None, raise_error=None):
        self._text = text
        self._raise = raise_error
        self.calls = 0

    async def call(self, **kwargs):
        self.calls += 1
        if self._raise:
            raise self._raise
        return FakeLLMResponse(self._text)


def _skill(skill_id="sk-1", success_count=0, title="Add two numbers",
           description="Adds two numbers together.", steps=None):
    return Skill(
        skill_id=skill_id,
        title=title,
        description=description,
        steps=steps or [{"tool": "calculator", "description": "add", "params": {"a": 2, "b": 3}}],
        tags=["math"],
        complexity="simple",
        success_count=success_count,
        failure_count=0,
    )


GOOD_CODE = """```python
def run(params: dict) -> dict:
    a = params.get("a", 0)
    b = params.get("b", 0)
    return {"sum": a + b}
```"""

BAD_IMPORT_CODE = """```python
import os

def run(params: dict) -> dict:
    return {"listing": os.listdir(".")}
```"""

RAISES_CODE = """```python
def run(params: dict) -> dict:
    raise ValueError("boom")
```"""

INFINITE_LOOP_CODE = """```python
def run(params: dict) -> dict:
    while True:
        pass
```"""


# ---------------------------------------------------------------------------
# check_invariants
# ---------------------------------------------------------------------------

def test_check_invariants_accepts_clean_code():
    code = "def run(params: dict) -> dict:\n    return {'ok': True}\n"
    assert check_invariants(code) == []


def test_check_invariants_rejects_disallowed_import():
    code = "import os\ndef run(params):\n    return {'x': os.getcwd()}\n"
    violations = check_invariants(code)
    assert any("disallowed import" in v for v in violations)


def test_check_invariants_rejects_dangerous_call():
    code = "def run(params):\n    return {'x': eval('1+1')}\n"
    violations = check_invariants(code)
    assert any("disallowed call: eval" in v for v in violations)


def test_check_invariants_rejects_dunder_attribute_access():
    code = "def run(params):\n    x = ().__class__\n    return {'x': str(x)}\n"
    violations = check_invariants(code)
    assert any("dunder attribute" in v for v in violations)


def test_check_invariants_rejects_wrong_function_name():
    code = "def do_it(params):\n    return {}\n"
    violations = check_invariants(code)
    assert any("top-level function named 'run'" in v for v in violations)


def test_check_invariants_rejects_wrong_arg_count():
    code = "def run(a, b):\n    return {}\n"
    violations = check_invariants(code)
    assert any("exactly one parameter" in v for v in violations)


def test_check_invariants_rejects_syntax_error():
    code = "def run(params:\n    return {}\n"
    violations = check_invariants(code)
    assert any("syntax error" in v for v in violations)


def test_check_invariants_allows_whitelisted_imports():
    code = "import math\ndef run(params):\n    return {'pi': math.pi}\n"
    assert check_invariants(code) == []


# ---------------------------------------------------------------------------
# extract_code
# ---------------------------------------------------------------------------

def test_extract_code_from_python_fence():
    text = "Here you go:\n```python\ndef run(params):\n    return {}\n```\nDone."
    code = extract_code(text)
    assert code == "def run(params):\n    return {}"


def test_extract_code_from_bare_fence():
    text = "```\ndef run(params):\n    return {}\n```"
    code = extract_code(text)
    assert code.startswith("def run")


def test_extract_code_from_unfenced_def():
    text = "def run(params):\n    return {}"
    assert extract_code(text) == text


def test_extract_code_returns_none_for_garbage():
    assert extract_code("Sure, I can help with that!") is None


def test_extract_code_returns_none_for_empty():
    assert extract_code("") is None
    assert extract_code(None) is None


# ---------------------------------------------------------------------------
# validate_in_sandbox — real subprocesses, no mocks
# ---------------------------------------------------------------------------

def test_validate_in_sandbox_accepts_good_code():
    code = "def run(params):\n    return {'sum': params['a'] + params['b']}\n"
    ok, detail = validate_in_sandbox(code, {"a": 2, "b": 3})
    assert ok is True
    assert '"sum": 5' in detail


def test_validate_in_sandbox_reports_raised_exception():
    code = "def run(params):\n    raise ValueError('boom')\n"
    ok, detail = validate_in_sandbox(code, {})
    assert ok is False
    assert "boom" in detail


def test_validate_in_sandbox_enforces_timeout():
    code = "def run(params):\n    while True:\n        pass\n"
    ok, detail = validate_in_sandbox(code, {}, timeout=1)
    assert ok is False
    assert "timed out" in detail


# ---------------------------------------------------------------------------
# ToolForge.should_forge
# ---------------------------------------------------------------------------

def _temp_forge():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    return ToolForge(router=FakeRouter(text=GOOD_CODE), forged_dir=Path(tmpdir))


def test_should_forge_false_below_threshold():
    forge = _temp_forge()
    skill = _skill(success_count=2)
    assert forge.should_forge(skill) is False


def test_should_forge_true_above_threshold():
    forge = _temp_forge()
    skill = _skill(success_count=4)
    assert forge.should_forge(skill) is True


def test_should_forge_false_once_already_forged():
    forge = _temp_forge()
    skill = _skill(success_count=10)
    forge._registry[skill.skill_id] = {"status": "forged"}
    assert forge.should_forge(skill) is False


def test_should_forge_false_after_max_attempts():
    forge = _temp_forge()
    skill = _skill(success_count=10)
    forge._registry[skill.skill_id] = {"status": "failed", "attempts": 2}
    assert forge.should_forge(skill) is False


# ---------------------------------------------------------------------------
# ToolForge.forge_from_skill
# ---------------------------------------------------------------------------

async def _test_forge_from_skill_success():
    forge = _temp_forge()
    skill = _skill(success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is True, result.error
    assert result.tool_name.startswith("forged_add_two_numbers")
    assert Path(result.code_path).exists()
    assert Path(result.code_path).read_text(encoding="utf-8").strip().startswith("def run")

    registry = forge._registry[skill.skill_id]
    assert registry["status"] == "forged"
    assert registry["tool_name"] == result.tool_name
    assert Path(forge._registry_path).exists()


async def _test_forge_from_skill_rejects_bad_import():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text=BAD_IMPORT_CODE), forged_dir=Path(tmpdir))
    skill = _skill(skill_id="sk-bad", success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is False
    assert "invariant check failed" in result.error
    assert forge._registry[skill.skill_id]["status"] == "failed"
    assert forge._registry[skill.skill_id]["attempts"] == 1


async def _test_forge_from_skill_rejects_no_code_block():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text="Sorry, I can't do that."), forged_dir=Path(tmpdir))
    skill = _skill(skill_id="sk-nocode", success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is False
    assert "no code block" in result.error


async def _test_forge_from_skill_handles_llm_failure():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(raise_error=RuntimeError("all providers down")), forged_dir=Path(tmpdir))
    skill = _skill(skill_id="sk-llmfail", success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is False
    assert "LLM call failed" in result.error


async def _test_forge_from_skill_rejects_code_that_raises():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text=RAISES_CODE), forged_dir=Path(tmpdir))
    skill = _skill(skill_id="sk-raises", success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is False
    assert "sandbox validation failed" in result.error
    assert "boom" in result.error


# ---------------------------------------------------------------------------
# End-to-end: forged tool actually registers and runs through ToolExecutor
# ---------------------------------------------------------------------------

async def _test_register_forged_tool_runs_through_executor():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text=GOOD_CODE), forged_dir=Path(tmpdir))
    skill = _skill(success_count=5)
    result = await forge.forge_from_skill(skill)
    assert result.ok is True, result.error

    executor = ToolExecutor()
    forge.register_forged_tool(executor, result.tool_name, result.code_path)
    assert result.tool_name in executor.tool_names

    params = {"a": 10, "b": 7}
    sig = _action_signature(result.tool_name, params)
    ctx = {"approved_actions": {sig}}
    tool_result = await executor.execute(result.tool_name, params, ctx)
    assert tool_result.success is True, tool_result.error
    assert '"sum": 17' in str(tool_result.output)


def test_register_forged_tool_runs_through_executor():
    asyncio.run(_test_register_forged_tool_runs_through_executor())


def test_forged_tool_requires_approval():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text=GOOD_CODE), forged_dir=Path(tmpdir))
    code_path = Path(tmpdir) / "forged_x.py"
    code_path.write_text("def run(params):\n    return {'ok': True}\n", encoding="utf-8")

    executor = ToolExecutor()
    forge.register_forged_tool(executor, "forged_x", str(code_path))
    assert executor._guardrails["forged_x"].require_approval is True


# ---------------------------------------------------------------------------
# load_forged_tools (restart-recovery)
# ---------------------------------------------------------------------------

async def _test_load_forged_tools_returns_only_forged_with_existing_code():
    tmpdir = tempfile.mkdtemp(prefix="alfred_forge_test_")
    forge = ToolForge(router=FakeRouter(text=GOOD_CODE), forged_dir=Path(tmpdir))

    ok_skill = _skill(skill_id="sk-ok", success_count=5, title="Ok Skill")
    result = await forge.forge_from_skill(ok_skill)
    assert result.ok is True

    # A failed attempt and a "forged" entry whose file got deleted should
    # both be excluded from what a restart re-registers.
    forge._registry["sk-failed"] = {"status": "failed", "attempts": 1}
    forge._registry["sk-missing-file"] = {
        "status": "forged", "tool_name": "forged_ghost", "code_path": "/nonexistent/ghost.py",
    }

    loaded = forge.load_forged_tools()
    tool_names = {entry["tool_name"] for entry in loaded}
    assert tool_names == {result.tool_name}


def test_load_forged_tools_returns_only_forged_with_existing_code():
    asyncio.run(_test_load_forged_tools_returns_only_forged_with_existing_code())


# ---------------------------------------------------------------------------
# SkillManager.record_skill_use
# ---------------------------------------------------------------------------

def _bare_manager():
    mgr = object.__new__(SkillManager)
    mgr._skills_cache = {}
    return mgr


def test_record_skill_use_increments_success_count():
    mgr = _bare_manager()
    fd, path = tempfile.mkstemp(suffix=".md", prefix="alfred_test_skill_")
    os.close(fd)
    skill = _skill(skill_id="s1", success_count=1)
    skill.path = path
    mgr._skills_cache[skill.skill_id] = skill
    try:
        ok = mgr.record_skill_use("s1", success=True)
        assert ok is True
        assert mgr._skills_cache["s1"].success_count == 2
        on_disk = Path(path).read_text(encoding="utf-8")
        assert "2/2" in on_disk or "Success Rate" in on_disk
    finally:
        os.unlink(path)


def test_record_skill_use_increments_failure_count():
    mgr = _bare_manager()
    fd, path = tempfile.mkstemp(suffix=".md", prefix="alfred_test_skill_")
    os.close(fd)
    skill = _skill(skill_id="s2", success_count=0)
    skill.path = path
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.record_skill_use("s2", success=False)
        assert mgr._skills_cache["s2"].failure_count == 1
        assert mgr._skills_cache["s2"].success_count == 0
    finally:
        os.unlink(path)


def test_record_skill_use_missing_skill_returns_false():
    mgr = _bare_manager()
    assert mgr.record_skill_use("nope", success=True) is False


def test_forge_threshold_matches_manifesto():
    """'Used successfully more than 3 times' -- crossing the threshold means
    success_count > 3, i.e. the 4th successful use is the one that
    triggers a forge attempt (record_skill_use runs before should_forge is
    checked in conversation.py's own turn-completion order)."""
    forge = _temp_forge()
    skill = _skill(success_count=3)
    assert forge.should_forge(skill) is False
    skill.success_count = 4
    assert forge.should_forge(skill) is True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_forge_from_skill_success():
    asyncio.run(_test_forge_from_skill_success())


def test_forge_from_skill_rejects_bad_import():
    asyncio.run(_test_forge_from_skill_rejects_bad_import())


def test_forge_from_skill_rejects_no_code_block():
    asyncio.run(_test_forge_from_skill_rejects_no_code_block())


def test_forge_from_skill_handles_llm_failure():
    asyncio.run(_test_forge_from_skill_handles_llm_failure())


def test_forge_from_skill_rejects_code_that_raises():
    asyncio.run(_test_forge_from_skill_rejects_code_that_raises())


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
