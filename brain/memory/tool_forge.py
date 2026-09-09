"""
Tool Forge — promotes a proven T2 skill into a standalone registered tool.

ROADMAP.md Week 3: "Finish Tool Forge: the markdown-skill -> executable-
Python conversion path (skill used 3+ times -> LLM-generated function ->
sandboxed validation -> registered tool). improve_skill()'s wiring from
this week is the down payment; this is the rest of it."

Pipeline:
    1. is_forge_candidate(skill)   -- used 3+ times, net-successful, not
                                       already forged, hasn't exhausted
                                       its forge attempts.
    2. generate_tool_code(...)     -- one LLM call asks for a single
                                       `async def run(params, ctx)` that
                                       replays the skill's steps.
    3. validate_tool_code(...)     -- static AST denylist, then a
                                       sandboxed dynamic smoke test against
                                       a fake tool_executor (no real tool
                                       ever actually runs during
                                       validation).
    4. register_forged_tool(...)   -- exec'd into a real ToolExecutor,
                                       gated by require_approval like
                                       shell/run_code, since it's
                                       LLM-authored code.

A forged tool never bypasses the guardrails a plain replay of the skill's
steps would hit -- it calls back into the same `ToolExecutor.execute()`
every other tool call goes through, so a forged tool whose skill happens
to include a `shell` step still needs approval for that step, every time.
Forging only removes the per-turn LLM planning cost of re-deriving "which
tools, in what order, with what params" -- it does not grant new trust.
"""
from __future__ import annotations

import ast
import asyncio
import builtins as _builtins_module
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .five_tier import T2_FORGED_TOOLS_DIR

FUNCTION_NAME = "run"
PROMOTION_THRESHOLD = 3
MAX_FORGE_ATTEMPTS = 3
VALIDATION_TIMEOUT_SECONDS = 5.0

REGISTRY_PATH = T2_FORGED_TOOLS_DIR / "registry.json"

# Denylist rather than a full allowlist -- matches the project's existing
# style for run_code/shell (see tool_executor.py's _DESTRUCTIVE_PATTERNS):
# a forged function is meant to be pure glue around ctx["tool_executor"],
# not a way to reach the filesystem/network/process table directly.
_DENIED_CALL_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "exit", "quit", "breakpoint", "input", "open",
}

# An allowlist of ordinary data-manipulation builtins, deliberately excluding
# eval/exec/open/__import__/compile/input/etc even though the static denylist
# above already blocks calling them -- an *empty* __builtins__ dict (this
# module's first cut) turned out to reject completely ordinary generated code
# too: `raise RuntimeError(...)`, `isinstance(x, dict)`, even `len(x)` all
# raise NameError with no builtins available at all. This is the actual
# runtime environment a forged function's code executes in, both during
# sandbox validation and for real once registered -- it has to have enough
# to write normal Python, just not enough to escape the sandbox.
_SAFE_BUILTIN_NAMES = {
    "dict", "list", "tuple", "set", "frozenset", "str", "int", "float",
    "bool", "bytes", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "min", "max", "sum", "abs", "round", "divmod",
    "isinstance", "issubclass", "any", "all", "print", "repr", "format",
    "True", "False", "None", "NotImplemented",
    "Exception", "BaseException", "ValueError", "KeyError", "TypeError",
    "RuntimeError", "StopIteration", "StopAsyncIteration", "IndexError",
    "AttributeError", "ArithmeticError", "ZeroDivisionError", "NameError",
    "LookupError", "OverflowError", "AssertionError",
}
_SAFE_BUILTINS: Dict[str, Any] = {
    name: getattr(_builtins_module, name)
    for name in _SAFE_BUILTIN_NAMES
    if hasattr(_builtins_module, name)
}


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def is_forge_candidate(skill: Any) -> bool:
    """A skill is ready to promote once it has genuinely proven itself:
    used at least PROMOTION_THRESHOLD times with at least twice as many
    successes as failures, not already forged, and hasn't exhausted its
    forge attempts (bad codegen shouldn't retry forever on every turn)."""
    if getattr(skill, "forged_tool", None):
        return False
    if getattr(skill, "forge_attempts", 0) >= MAX_FORGE_ATTEMPTS:
        return False
    success = getattr(skill, "success_count", 0)
    failure = getattr(skill, "failure_count", 0)
    total = success + failure
    if total < PROMOTION_THRESHOLD:
        return False
    return success >= 2 * failure


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

_CODEGEN_SYSTEM_PROMPT = f"""You convert a proven multi-step Alfred skill into a single reusable Python tool function.

Output ONLY a fenced python code block containing exactly one top-level function:

    async def {FUNCTION_NAME}(params: dict, ctx: dict) -> dict:
        ...

Hard rules:
- The ONLY way to take any action is `await ctx["tool_executor"].execute(tool_name, step_params, ctx["tool_context"])`, which returns a dict with at least a "success" key. You may call it once per step, in order.
- Use `params` (the arguments the caller passed this forged tool) to fill in the step params that should vary between calls; keep the rest identical to the skill's recorded steps.
- Return a dict, e.g. {{"success": True, "output": "..."}} summarizing what happened.
- No imports, no `open`/`eval`/`exec`/`compile`/`__import__`, no dunder attribute access, no network/file/process access outside of ctx["tool_executor"].execute.
- No comments needed. No text outside the single code block.
"""


def _build_codegen_prompt(skill: Any) -> str:
    steps_desc = "\n".join(
        f"{i + 1}. tool={s.get('tool')!r} params={s.get('params') or {}!r}"
        for i, s in enumerate(getattr(skill, "steps", []))
    )
    return (
        f"Skill title: {skill.title}\n"
        f"Skill description: {skill.description}\n"
        f"Recorded steps (replay these, in order, via ctx['tool_executor'].execute):\n"
        f"{steps_desc}\n"
    )


def _extract_code_block(text: str) -> Optional[str]:
    """Pull the first fenced code block's contents out of an LLM response,
    tolerating a bare (unfenced) function body as a fallback."""
    if not text:
        return None
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    if f"async def {FUNCTION_NAME}(" in text:
        return text.strip()
    return None


async def generate_tool_code(skill: Any, router: Any) -> Optional[str]:
    """One LLM call asking for a single async function implementing the
    skill. Returns the extracted source, or None if the model produced
    nothing usable -- callers must still run it through validate_tool_code
    before trusting it."""
    try:
        resp = await router.call(
            system_prompt=_CODEGEN_SYSTEM_PROMPT,
            user_message=_build_codegen_prompt(skill),
            max_tokens=600,
            temperature=0.1,
        )
    except Exception:
        return None
    return _extract_code_block(getattr(resp, "text", None) or "")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ForgeValidation:
    ok: bool
    reason: str = ""


def _static_validate(code: str) -> ForgeValidation:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return ForgeValidation(False, f"syntax error: {e}")

    fn_defs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    matching = [n for n in fn_defs if n.name == FUNCTION_NAME]
    if not matching:
        return ForgeValidation(False, f"no top-level `async def {FUNCTION_NAME}(...)` found")
    fn = matching[0]
    arg_names = [a.arg for a in fn.args.args]
    if arg_names != ["params", "ctx"]:
        return ForgeValidation(False, f"expected signature (params, ctx), got {arg_names}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return ForgeValidation(False, "imports are not allowed")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return ForgeValidation(False, "global/nonlocal are not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return ForgeValidation(False, f"dunder name not allowed: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return ForgeValidation(False, f"dunder attribute not allowed: {node.attr}")
        if isinstance(node, ast.Call):
            target = node.func
            name = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name in _DENIED_CALL_NAMES:
                return ForgeValidation(False, f"call to {name!r} is not allowed")

    return ForgeValidation(True)


class _RealExecutorAdapter:
    """Wraps a real ToolExecutor so a forged function sees the same
    dict-shaped `execute()` result during a real run as it did during
    sandbox validation (_FakeToolExecutor below) -- the real
    ToolExecutor.execute() returns a ToolResult object, not a dict, and a
    generated function written against the dict contract would otherwise
    crash on `result["success"]` the first time it actually ran."""

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def execute(self, tool_name, params, ctx) -> Dict[str, Any]:
        result = await self._executor.execute(tool_name, params, ctx)
        return {"success": result.success, "output": result.output, "error": result.error}


class _FakeToolExecutor:
    """Sandbox stand-in for validation only -- records what a candidate
    function *would* call without ever running a real tool. `execute()`
    always reports success so a function with genuinely-correct control
    flow passes; a function that mishandles a failure path is not this
    check's job (the real ToolExecutor still gates every real call once
    the forged tool actually runs).

    `delay` exists only so tests can exercise the VALIDATION_TIMEOUT_SECONDS
    path deterministically: a candidate function's own code can never
    legitimately hang (no imports means no real sleep/I/O it could reach),
    so the one realistic way validation ever actually times out is a slow
    ctx["tool_executor"].execute() call underneath it."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: List[Any] = []
        self._delay = delay

    async def execute(self, tool_name, params, ctx):
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((tool_name, params))
        return {"success": True, "output": "sandbox-ok"}


def _sample_params(skill: Any) -> Dict[str, Any]:
    steps = getattr(skill, "steps", None) or []
    if steps and isinstance(steps[0], dict):
        return dict(steps[0].get("params") or {})
    return {}


async def _dynamic_validate(
    code: str, skill: Any, _fake_executor_delay: float = 0.0
) -> ForgeValidation:
    namespace: Dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    try:
        exec(compile(code, "<forged_tool>", "exec"), namespace)
    except Exception as e:
        return ForgeValidation(False, f"failed to compile/exec: {e}")

    fn = namespace.get(FUNCTION_NAME)
    if not callable(fn):
        return ForgeValidation(False, f"`{FUNCTION_NAME}` not defined after exec")

    fake_executor = _FakeToolExecutor(delay=_fake_executor_delay)
    fake_ctx = {"tool_executor": fake_executor, "tool_context": {}}
    try:
        result = await asyncio.wait_for(
            fn(_sample_params(skill), fake_ctx), timeout=VALIDATION_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return ForgeValidation(False, f"timed out after {VALIDATION_TIMEOUT_SECONDS}s")
    except Exception as e:
        return ForgeValidation(False, f"raised during sandbox run: {e}")

    if not isinstance(result, dict):
        return ForgeValidation(False, f"must return a dict, got {type(result).__name__}")

    return ForgeValidation(True)


async def validate_tool_code(code: str, skill: Any) -> ForgeValidation:
    static = _static_validate(code)
    if not static.ok:
        return static
    return await _dynamic_validate(code, skill)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _safe_tool_name(skill: Any) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", skill.title.strip().lower())[:40].strip("_")
    return f"forged_{base or 'tool'}_{skill.skill_id[:8]}"


def _load_registry() -> Dict[str, Dict[str, Any]]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    T2_FORGED_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _persist(tool_name: str, code: str, skill: Any) -> Path:
    T2_FORGED_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    code_path = T2_FORGED_TOOLS_DIR / f"{tool_name}.py"
    code_path.write_text(code, encoding="utf-8")

    registry = _load_registry()
    registry[tool_name] = {
        "skill_id": skill.skill_id,
        "title": skill.title,
        "description": skill.description,
        "code_path": str(code_path),
        "created_at": datetime.now().isoformat(),
    }
    _save_registry(registry)
    return code_path


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _make_handler(code: str) -> Optional[Callable]:
    """Exec the validated source once and wrap its `run` in a
    ToolExecutor-compatible handler. Uses the same restricted builtins as
    dynamic validation -- validation proves the code behaves against a
    fake executor, it does not make re-running it with an empty
    __builtins__ redundant, since exec() happens fresh here in the real
    process."""
    namespace: Dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    try:
        exec(compile(code, "<forged_tool>", "exec"), namespace)
    except Exception:
        return None
    fn = namespace.get(FUNCTION_NAME)
    if not callable(fn):
        return None

    async def _handler(params: Dict[str, Any], ctx: Dict[str, Any]):
        from ..v2.tool_executor import ToolResult

        real_executor = ctx.get("tool_executor")
        adapter = _RealExecutorAdapter(real_executor) if real_executor is not None else None
        forged_ctx = {"tool_executor": adapter, "tool_context": ctx}
        try:
            result = await fn(params or {}, forged_ctx)
        except Exception as e:
            return ToolResult(success=False, error=f"forged tool raised: {e}")
        if not isinstance(result, dict):
            return ToolResult(success=False, error="forged tool returned a non-dict result")
        success = bool(result.get("success", True))
        if success:
            return ToolResult(success=True, output=result.get("output", ""))
        return ToolResult(success=False, error=str(result.get("error") or "forged tool reported failure"))

    return _handler


def register_forged_tool(executor: Any, tool_name: str, code: str) -> bool:
    """Register one forged tool into a live ToolExecutor. Requires
    approval, same trust tier as run_code/shell/install_mcp_server -- it's
    LLM-authored code, even though each step it takes still re-clears the
    real ToolExecutor's own guardrails."""
    from ..v2.tool_executor import Guardrails

    handler = _make_handler(code)
    if handler is None:
        return False
    executor.register(tool_name, handler, guardrails=Guardrails(require_approval=True))
    return True


def forged_tool_schema(tool_name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Schema dict shaped like conversation.py's _mcp_tool_schemas entries,
    for merging into _get_tool_descriptions()."""
    return {
        "description": (
            (entry.get("description") or f"Forged tool: {entry.get('title', tool_name)}")
            + " (Requires approval before running -- auto-generated from a learned skill.)"
        ),
        "params": {},
    }


def load_persisted_forged_tools(executor: Any) -> Dict[str, Dict[str, Any]]:
    """Re-register every previously-forged tool into a fresh ToolExecutor
    (startup path, mirrors connect_mcp_servers()). Returns the schema dict
    to merge into _get_tool_descriptions(); a tool whose code file went
    missing or no longer registers cleanly is skipped, not fatal."""
    registry = _load_registry()
    schemas: Dict[str, Dict[str, Any]] = {}
    for tool_name, entry in registry.items():
        code_path = Path(entry.get("code_path", ""))
        if not code_path.exists():
            continue
        try:
            code = code_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if register_forged_tool(executor, tool_name, code):
            schemas[tool_name] = forged_tool_schema(tool_name, entry)
    return schemas


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class ForgeResult:
    success: bool
    tool_name: Optional[str] = None
    schema: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


async def forge_from_skill(skill: Any, skill_manager: Any, router: Any, executor: Any) -> ForgeResult:
    """Attempt to promote one skill to a registered tool. Always leaves
    skill_manager's on-disk/cached state consistent with the outcome:
    forged_tool set on success, forge_attempts incremented on failure."""
    code = await generate_tool_code(skill, router)
    if not code:
        skill_manager.record_forge_attempt(skill.skill_id)
        return ForgeResult(False, reason="LLM produced no usable code")

    validation = await validate_tool_code(code, skill)
    if not validation.ok:
        skill_manager.record_forge_attempt(skill.skill_id)
        return ForgeResult(False, reason=validation.reason)

    tool_name = _safe_tool_name(skill)
    _persist(tool_name, code, skill)
    if not register_forged_tool(executor, tool_name, code):
        skill_manager.record_forge_attempt(skill.skill_id)
        return ForgeResult(False, reason="registration failed after validation passed")

    skill_manager.mark_forged(skill.skill_id, tool_name)
    schema = forged_tool_schema(tool_name, {"title": skill.title, "description": skill.description})
    return ForgeResult(True, tool_name=tool_name, schema=schema)


async def run_forge_pass(skill_manager: Any, executor: Any, router: Any) -> Dict[str, Dict[str, Any]]:
    """Scan every known skill for forge candidates and attempt each one.
    Returns {tool_name: schema} for whatever got newly forged this pass."""
    new_schemas: Dict[str, Dict[str, Any]] = {}
    for skill in skill_manager.get_all_skills():
        if not is_forge_candidate(skill):
            continue
        result = await forge_from_skill(skill, skill_manager, router, executor)
        if result.success and result.tool_name:
            new_schemas[result.tool_name] = result.schema
    return new_schemas
