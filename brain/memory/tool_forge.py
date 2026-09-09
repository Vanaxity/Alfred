"""
Tool Forge — promote a frequently-reused Skill into a directly-registered tool.

ROADMAP.md Phase 3 spec: a markdown skill used 3+ times (SkillManager's
FORGE_THRESHOLD / should_forge()) gets converted from "the LLM re-plans this
from its steps every time it's matched" into "one deterministic tool call",
via: LLM-generated function -> sandboxed validation -> registered tool.
`improve_skill()`'s existing wiring (skill_manager.py) was the down payment
this closes out the rest of.

Design constraint that shapes everything below: the generated function's
*only* capability is calling already-registered, already-guarded tools via
`ctx["tool_executor"].execute(...)` -- the same call every built-in tool
already goes through, guardrails and approval included. It never gets raw
file/network/shell access of its own. That keeps a forged tool's blast
radius bounded by whatever its underlying steps already required (a skill
with a `shell` step still needs human approval every time it fires) instead
of handing an LLM a blank check to write arbitrary code that then runs
unattended forever after.

Validation is defense in depth, in order:
  1. AST safety check (this module, no execution) -- exactly one top-level
     `async def run(params, ctx):`, no imports, no dunder attribute access,
     no forbidden names (exec/eval/open/__import__/getattr/...).
  2. A sandboxed dry run in a subprocess, with a fake tool_executor that
     records calls instead of touching anything real, restricted builtins,
     no environment, and a hard timeout -- catches a crash or a hang before
     this code is ever trusted with the live executor.
  3. A tool-name allowlist check: the dry run must not call any tool the
     original skill's steps didn't already use -- a forged tool cannot
     acquire a capability the skill it was forged from never had.

This is a best-effort sandbox suited to a personal, single-user assistant,
not a hardened multi-tenant execution boundary (no seccomp/gVisor/nsjail) --
documented here rather than implied, since "sandboxed" can otherwise read
as a stronger guarantee than what this actually provides.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Optional, Set

from ..v2.tool_executor import ToolResult

SANDBOX_TIMEOUT_SECONDS = 10
RUNTIME_TIMEOUT_SECONDS = 30


@dataclass
class ForgeResult:
    ok: bool
    tool_name: str = ""
    code: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _build_forge_prompt(skill: Any) -> str:
    steps_desc = "\n".join(
        f"{i + 1}. tool={step.get('tool')!r} "
        f"params={json.dumps(step.get('params') or {}, default=str)} "
        f"-- {step.get('description', '')}"
        for i, step in enumerate(skill.steps)
    )
    return f"""Convert this learned skill into a single Python async function
that replays its steps directly, instead of an LLM re-planning it from
scratch every time it's matched.

Skill: {skill.title}
Description: {skill.description}
Steps (in order):
{steps_desc}

Write EXACTLY one top-level Python statement: `async def run(params, ctx):`

Rules (a static checker enforces every one of these -- code that breaks any
of them will be rejected):
- No imports, no other top-level statements, no class or lambda definitions.
- The ONLY way to take an action is:
      result = await ctx["tool_executor"].execute(<tool_name_str>, <params_dict>, ctx)
  using the same tool names shown above, once per step. `params_dict` may
  reuse a value from the `params` dict passed into run() instead of a
  hardcoded literal, if a step needs something the caller supplies.
- `result` has `.success` (bool), `.output`, `.error` attributes. Check
  `.success` after each call and stop early on failure.
- Return a plain Python dict: {{"success": bool, "output": ..., "error": ...}}.
  Never return a ToolResult object, never raise an exception yourself.
- No file, network, subprocess, or shell access other than through
  ctx["tool_executor"].execute() -- no open(), no os, no import of anything.

Reply with ONLY the Python code. No markdown code fences, no prose, no
explanation before or after.
"""


def _extract_code(text: str) -> str:
    text = (text or "").strip()
    if "```" in text:
        parts = text.split("```")
        # parts alternate prose/code; the first fenced block is what we want.
        for part in parts[1:]:
            candidate = part
            if candidate.startswith("python"):
                candidate = candidate[len("python"):]
            candidate = candidate.strip()
            if candidate:
                return candidate
    return text


# ---------------------------------------------------------------------------
# 1. AST safety check
# ---------------------------------------------------------------------------

_FORBIDDEN_NAMES: Set[str] = {
    "__import__", "exec", "eval", "compile", "open", "input", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "exit", "quit",
    "help", "dir", "breakpoint", "memoryview", "__build_class__",
}


def _ast_safety_check(code: str) -> Optional[str]:
    """Return None if `code` passes static safety checks, else a reason."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error: {e}"

    top_level_funcs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    other_top_level = [n for n in tree.body if not isinstance(n, ast.AsyncFunctionDef)]
    if other_top_level:
        return "only one top-level statement is allowed: `async def run(params, ctx):`"
    if len(top_level_funcs) != 1 or top_level_funcs[0].name != "run":
        return "code must define exactly one top-level `async def run(params, ctx):`"

    fn = top_level_funcs[0]
    arg_names = [a.arg for a in fn.args.args]
    if arg_names != ["params", "ctx"]:
        return "run() must take exactly (params, ctx)"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "imports are not allowed in a forged tool"
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return "global/nonlocal statements are not allowed"
        if isinstance(node, (ast.ClassDef, ast.Lambda)):
            return "class/lambda definitions are not allowed"
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            return f"forbidden name used: {node.id}"
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"dunder attribute access is not allowed: {node.attr}"

    return None


# ---------------------------------------------------------------------------
# 2. Sandboxed dry run (subprocess, fake executor, restricted builtins)
# ---------------------------------------------------------------------------

_SAFE_BUILTIN_NAMES = (
    "True", "False", "None", "len", "range", "enumerate", "isinstance",
    "str", "int", "float", "bool", "dict", "list", "tuple", "set",
    "min", "max", "sum", "any", "all", "sorted", "reversed", "zip", "abs",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "StopIteration", "print",
)

_HARNESS_TEMPLATE = """\
import asyncio, json, builtins

_safe_names = {safe_names!r}
_safe_builtins = {{n: getattr(builtins, n) for n in _safe_names if hasattr(builtins, n)}}

_CODE = {code!r}


class _ToolResult:
    def __init__(self, success, output=None, error=None):
        self.success = success
        self.output = output
        self.error = error


class _DryRunExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, tool_name, params, ctx=None):
        self.calls.append({{"tool": tool_name, "params": dict(params or {{}})}})
        return _ToolResult(True, output="[sandbox] " + str(tool_name) + " ok")


async def _main():
    ns = {{}}
    exec(compile(_CODE, "<forged>", "exec"), {{"__builtins__": _safe_builtins}}, ns)
    run_fn = ns["run"]
    executor = _DryRunExecutor()
    ctx = {{"tool_executor": executor}}
    result = await run_fn({test_params!r}, ctx)
    print(json.dumps({{"ok": True, "result": result, "calls": executor.calls}}))


try:
    asyncio.run(_main())
except Exception as e:
    print(json.dumps({{"ok": False, "error": type(e).__name__ + ": " + str(e)}}))
"""


def _sandbox_dry_run(code: str, skill: Any) -> Optional[str]:
    """Run `code` once in an isolated subprocess against a fake tool
    executor. Returns None on success, else a human-readable reason it
    failed validation.
    """
    harness = _HARNESS_TEMPLATE.format(
        safe_names=list(_SAFE_BUILTIN_NAMES),
        code=code,
        test_params={},
    )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(harness)
            tmp_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, text=True,
                timeout=SANDBOX_TIMEOUT_SECONDS,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except subprocess.TimeoutExpired:
            return f"sandbox timed out after {SANDBOX_TIMEOUT_SECONDS}s (possible infinite loop)"

        stdout = (proc.stdout or "").strip()
        last_line = stdout.splitlines()[-1] if stdout else ""
        try:
            payload = json.loads(last_line)
        except (json.JSONDecodeError, IndexError):
            return f"sandbox produced no parseable result (stderr: {(proc.stderr or '')[:300]})"

        if not payload.get("ok"):
            return f"sandbox run raised: {payload.get('error', 'unknown error')}"

        result = payload.get("result")
        if not isinstance(result, dict) or "success" not in result:
            return f"run() must return a dict with a 'success' key, got: {result!r}"

        allowed_tools = {step.get("tool") for step in skill.steps if step.get("tool")}
        used_tools = {c.get("tool") for c in payload.get("calls", [])}
        extra = used_tools - allowed_tools
        if extra:
            return (
                f"forged code called tool(s) the original skill never used: "
                f"{sorted(extra)} (allowed: {sorted(allowed_tools)})"
            )

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return None


# ---------------------------------------------------------------------------
# Orchestration: LLM generation -> validation
# ---------------------------------------------------------------------------

async def forge_tool_from_skill(skill: Any, router: Any) -> ForgeResult:
    """Ask `router` to generate a replay function for `skill`, validate it,
    and return a ForgeResult. Registration into a live ToolExecutor is the
    caller's job (see make_forged_handler below) -- this function never
    touches process-wide state, so it's safe to call speculatively and
    discard the result.
    """
    if not skill.steps:
        return ForgeResult(ok=False, reason="skill has no steps to forge")

    prompt = _build_forge_prompt(skill)
    try:
        resp = await router.call(
            system_prompt=(
                "You write small, safe Python functions that replay a fixed "
                "tool-call sequence. Output only code."
            ),
            user_message=prompt,
            messages=[],
            max_tokens=600,
            temperature=0.0,
        )
    except Exception as e:
        return ForgeResult(ok=False, reason=f"LLM call failed: {e}")

    code = _extract_code(getattr(resp, "text", None) or "")
    if not code:
        return ForgeResult(ok=False, reason="LLM returned no usable code")

    ast_error = _ast_safety_check(code)
    if ast_error:
        return ForgeResult(ok=False, code=code, reason=f"AST safety check failed: {ast_error}")

    sandbox_error = _sandbox_dry_run(code, skill)
    if sandbox_error:
        return ForgeResult(ok=False, code=code, reason=f"sandbox validation failed: {sandbox_error}")

    return ForgeResult(ok=True, tool_name=f"skill_{skill.skill_id}", code=code)


# ---------------------------------------------------------------------------
# Live handler factory
# ---------------------------------------------------------------------------

def make_forged_handler(
    code: str, tool_name: str
) -> Callable[[Dict[str, Any], Dict[str, Any]], Coroutine[Any, Any, ToolResult]]:
    """Wrap validated forged code into a ToolExecutor-compatible handler.

    Runs in-process (unlike the subprocess sandbox above) because it needs
    the real, live ctx["tool_executor"] to actually do anything -- a
    subprocess can't share that object. Safety at this point rests on the
    validation already performed once before this handler is ever
    registered: restricted builtins here are a second line of defense, not
    the only one.
    """
    compiled = compile(code, f"<forged:{tool_name}>", "exec")
    safe_builtins = {
        n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(builtins, n)
    }

    async def handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        import asyncio

        ns: Dict[str, Any] = {}
        try:
            exec(compiled, {"__builtins__": safe_builtins}, ns)
            run_fn = ns["run"]
            raw = await asyncio.wait_for(run_fn(params, ctx), timeout=RUNTIME_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Forged tool '{tool_name}' timed out after {RUNTIME_TIMEOUT_SECONDS}s",
                tool_name=tool_name,
            )
        except Exception as e:
            return ToolResult(
                success=False, error=f"Forged tool '{tool_name}' crashed: {e}", tool_name=tool_name,
            )

        if not isinstance(raw, dict):
            return ToolResult(
                success=False,
                error=f"Forged tool '{tool_name}' returned {type(raw).__name__}, expected dict",
                tool_name=tool_name,
            )
        return ToolResult(
            success=bool(raw.get("success")),
            output=raw.get("output"),
            error=raw.get("error"),
            tool_name=tool_name,
        )

    return handler
