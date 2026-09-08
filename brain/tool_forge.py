"""
Tool Forge — Skill (markdown) -> Code (Python tool), per the manifesto's
Phase 3 spec:

    "When a skill is used successfully more than 3 times, Alfred
    automatically converts its markdown procedure into a Python function
    (via an LLM), validates it in a subprocess sandbox with an invariant
    checker, and registers it as a new tool."

`SkillManager.improve_skill()` (shipped earlier this week) is the "patch a
skill's steps" half of self-improvement; this is the "graduate a skill into
a real executable capability" half. Three stages, each of which can refuse
to proceed rather than trust the previous one:

    1. build_forge_prompt() / extract_code() — ask an LLM for one
       self-contained `def run(params: dict) -> dict` function, pull the
       code out of its response.
    2. check_invariants() — static AST check (the "invariant checker"):
       only a small stdlib allowlist may be imported, no eval/exec/open/
       getattr-style sandbox-escape primitives, no dunder attribute access
       (blocks the classic `().__class__.__bases__` trick).
    3. validate_in_sandbox() — actually run it, once, in a subprocess with
       a timeout, before it's ever trusted as a registered tool.

Only a skill that clears all three gets written to `forged_tools/` and
handed back to the caller (conversation.py) to register through the same
`ToolExecutor.register()` every built-in and MCP tool already uses —
gated behind `require_approval=True`, the same trust tier as `shell`/
`run_code`/`install_mcp_server`: this is LLM-generated code, not a vetted
built-in.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

# "used successfully more than 3 times" — the manifesto's own threshold.
FORGE_THRESHOLD = 3
# Stop retrying a skill whose forge attempts keep failing (a bad LLM
# response or an inherently non-forgeable skill) rather than re-running the
# LLM call + sandbox every single turn it's matched forever.
MAX_FORGE_ATTEMPTS = 2
SANDBOX_TIMEOUT_SECONDS = 10

def _default_forged_dir() -> Path:
    """Forged tools are generated, per-installation artifacts, exactly like
    T2 skills/T3 episodes/the T4 profile -- they belong in Sam's Obsidian
    vault alongside the rest of Alfred's memory tiers, not committed into
    this git repo. Imported lazily (not at module level) so importing
    tool_forge.py for its pure functions (check_invariants, extract_code,
    validate_in_sandbox) never requires the five-tier memory module's own
    dependencies unless a ToolForge instance is actually constructed."""
    from .memory.five_tier import MEMORY_DIR

    return MEMORY_DIR / "ForgedTools"


REGISTRY_FILENAME = "forged_tools.json"

# Deliberately small: this is pure-computation glue code converted from a
# markdown procedure, not a place that needs filesystem/network/process
# access — those stay behind Alfred's existing approval-gated tools.
ALLOWED_IMPORTS = {
    "math", "re", "json", "datetime", "statistics", "itertools",
    "collections", "string", "textwrap",
}

# Anything that can read arbitrary code, touch the filesystem, or escape a
# restricted namespace. Not a security boundary against a determined
# attacker with subprocess access — real isolation is the subprocess
# sandbox in validate_in_sandbox() — but a reasonable first gate against an
# LLM completion that reaches for e.g. `open()` or `eval()` if the prompt's
# own "pure computation only" instruction gets ignored.
_DANGEROUS_CALL_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "exit", "quit",
}

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Stage 2: the invariant checker
# ---------------------------------------------------------------------------

class _InvariantVisitor(ast.NodeVisitor):
    """Walks a candidate function's AST for anything that would let it
    escape the sandbox, before it's ever executed."""

    def __init__(self) -> None:
        self.violations: List[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                self.violations.append(f"disallowed import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_IMPORTS:
            self.violations.append(f"disallowed import: {node.module}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in _DANGEROUS_CALL_NAMES:
            self.violations.append(f"disallowed call: {name}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            self.violations.append(f"disallowed dunder attribute access: {node.attr}")
        self.generic_visit(node)


def check_invariants(code: str) -> List[str]:
    """Static pre-execution check. Returns a list of violations — empty
    means it passed. Never raises; a SyntaxError is itself a violation."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e}"]

    run_funcs = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"
    ]
    if len(run_funcs) != 1:
        return [
            "expected exactly one top-level function named 'run', found "
            f"{len(run_funcs)}"
        ]
    run_func = run_funcs[0]
    n_args = len(run_func.args.args)
    if n_args != 1:
        return [f"'run' must take exactly one parameter, found {n_args}"]

    visitor = _InvariantVisitor()
    visitor.visit(tree)
    return visitor.violations


# ---------------------------------------------------------------------------
# Stage 1: LLM prompt + code extraction
# ---------------------------------------------------------------------------

def build_forge_prompt(skill: Any) -> str:
    steps_desc = "\n".join(
        f"{i + 1}. tool={s.get('tool')!r} description={s.get('description')!r} "
        f"params={s.get('params')!r}"
        for i, s in enumerate(skill.steps)
    ) or "(no steps recorded)"

    return f"""Convert this learned skill into a single, self-contained Python function.

Skill: {skill.title}
Description: {skill.description}
Steps:
{steps_desc}

Requirements:
- Output ONLY one fenced ```python code block, nothing else -- no prose before or after it.
- Define exactly one top-level function: def run(params: dict) -> dict
- `params` mirrors the shape of this skill's step params.
- You may only import from: {", ".join(sorted(ALLOWED_IMPORTS))}. No other imports.
- No file, network, subprocess, or system access of any kind -- pure computation only.
- No eval/exec/compile/__import__/open/getattr/setattr/globals/locals and no dunder
  attribute access (e.g. no `__class__`, `__globals__`).
- Return a JSON-serializable dict describing the result.
- If this skill's steps genuinely require an external tool or service this function
  cannot reach (an API call, a file write, etc.), return
  {{"error": "requires <tool name>, not convertible to pure code"}} instead of
  faking that behavior.
"""


def extract_code(llm_text: Optional[str]) -> Optional[str]:
    if not llm_text:
        return None
    m = _CODE_FENCE_RE.search(llm_text)
    if m:
        code = m.group(1).strip()
        return code or None
    stripped = llm_text.strip()
    if stripped.startswith("def "):
        return stripped
    return None


# ---------------------------------------------------------------------------
# Stage 3: subprocess sandbox
# ---------------------------------------------------------------------------

_HARNESS_TEMPLATE = """\
{code}

if __name__ == "__main__":
    import json as _json, sys as _sys
    _params = _json.loads({params_json!r})
    try:
        _result = run(_params)
        _sys.stdout.write("__FORGE_OK__" + _json.dumps(_result))
    except Exception as _e:
        _sys.stderr.write("__FORGE_ERR__" + str(_e))
        _sys.exit(1)
"""


def validate_in_sandbox(
    code: str, sample_params: Dict[str, Any], timeout: int = SANDBOX_TIMEOUT_SECONDS
) -> Tuple[bool, str]:
    """Run `code` (already invariant-checked) against `sample_params` in a
    real, timeout-bounded subprocess -- the same isolation model
    `handle_run_code` already uses for `run_code`, applied here at both
    forge-validation time and every actual call time (see
    ToolForge.make_handler). Synchronous/blocking by design; callers in an
    event loop should run it via asyncio.to_thread."""
    params_json = json.dumps(sample_params or {})
    harness = _HARNESS_TEMPLATE.format(code=code, params_json=params_json)

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(harness)
            tmp = f.name
        r = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            err = r.stderr or f"exit code {r.returncode}"
            if "__FORGE_ERR__" in err:
                err = err.split("__FORGE_ERR__", 1)[-1]
            return False, err.strip()[:500]
        if "__FORGE_OK__" not in r.stdout:
            return False, "sandbox run produced no result marker"
        return True, r.stdout.split("__FORGE_OK__", 1)[-1].strip()[:500]
    except subprocess.TimeoutExpired:
        return False, f"sandbox execution timed out ({timeout}s)"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# ToolForge
# ---------------------------------------------------------------------------

def _make_tool_name(skill: Any) -> str:
    slug = _SLUG_RE.sub("_", skill.title.lower()).strip("_")[:40] or "skill"
    return f"forged_{slug}_{skill.skill_id[:6]}"


@dataclass
class ForgeResult:
    ok: bool
    tool_name: Optional[str] = None
    code_path: Optional[str] = None
    error: Optional[str] = None


class ToolForge:
    """Owns the forged-tool registry (which skills have been converted, or
    tried and failed) and drives a skill through all three forge stages."""

    def __init__(self, router: Any, forged_dir: Optional[Path] = None) -> None:
        self._router = router
        self._forged_dir = forged_dir or _default_forged_dir()
        self._registry_path = self._forged_dir / REGISTRY_FILENAME
        self._registry: Dict[str, Dict[str, Any]] = self._load_registry()

    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        if self._registry_path.exists():
            try:
                return json.loads(self._registry_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_registry(self) -> None:
        self._forged_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(self._registry, indent=2), encoding="utf-8"
        )

    def should_forge(self, skill: Any) -> bool:
        """True only for a skill that has actually crossed the manifesto's
        usage threshold, isn't already forged, and hasn't already failed
        forging MAX_FORGE_ATTEMPTS times."""
        entry = self._registry.get(skill.skill_id)
        if entry:
            if entry.get("status") == "forged":
                return False
            if entry.get("attempts", 0) >= MAX_FORGE_ATTEMPTS:
                return False
        return skill.success_count > FORGE_THRESHOLD

    def _record_attempt(self, skill_id: str, error: str) -> None:
        entry = self._registry.setdefault(skill_id, {"attempts": 0})
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["status"] = "failed"
        entry["last_error"] = error
        self._save_registry()

    async def forge_from_skill(self, skill: Any) -> ForgeResult:
        prompt = build_forge_prompt(skill)
        try:
            resp = await self._router.call(
                system_prompt="You are a precise Python code generator. Output only code.",
                user_message=prompt,
                max_tokens=800,
                temperature=0.1,
            )
        except Exception as e:
            error = f"LLM call failed: {e}"
            self._record_attempt(skill.skill_id, error)
            return ForgeResult(ok=False, error=error)

        code = extract_code(getattr(resp, "text", None))
        if not code:
            error = "no code block in LLM response"
            self._record_attempt(skill.skill_id, error)
            return ForgeResult(ok=False, error=error)

        violations = check_invariants(code)
        if violations:
            error = f"invariant check failed: {'; '.join(violations)}"
            self._record_attempt(skill.skill_id, error)
            return ForgeResult(ok=False, error=error)

        sample_params = (skill.steps[0].get("params") or {}) if skill.steps else {}
        ok, detail = await asyncio.to_thread(validate_in_sandbox, code, sample_params)
        if not ok:
            error = f"sandbox validation failed: {detail}"
            self._record_attempt(skill.skill_id, error)
            return ForgeResult(ok=False, error=error)

        tool_name = _make_tool_name(skill)
        self._forged_dir.mkdir(parents=True, exist_ok=True)
        code_path = self._forged_dir / f"{tool_name}.py"
        code_path.write_text(code, encoding="utf-8")

        prior_attempts = self._registry.get(skill.skill_id, {}).get("attempts", 0)
        self._registry[skill.skill_id] = {
            "tool_name": tool_name,
            "code_path": str(code_path),
            "description": skill.description,
            "status": "forged",
            "forged_at": datetime.now().isoformat(),
            "attempts": prior_attempts,
        }
        self._save_registry()
        return ForgeResult(ok=True, tool_name=tool_name, code_path=str(code_path))

    def make_handler(self, code_path: str) -> Callable[[Dict[str, Any], Dict[str, Any]], Coroutine]:
        """A ToolExecutor-compatible handler for a forged tool. Re-runs the
        forged code through the same subprocess sandbox on every call
        (not just at forge time) -- defense in depth, and consistent with
        how `run_code` is trusted: sandboxed at call time, not just once."""
        from .v2.tool_executor import ToolResult

        async def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
            try:
                code = Path(code_path).read_text(encoding="utf-8")
            except Exception as e:
                return ToolResult(success=False, error=f"forged tool code missing: {e}")
            ok, detail = await asyncio.to_thread(validate_in_sandbox, code, params or {})
            if not ok:
                return ToolResult(success=False, error=detail)
            return ToolResult(success=True, output=detail)

        return _handler

    def register_forged_tool(self, executor: Any, tool_name: str, code_path: str) -> None:
        """Registers through the same ToolExecutor.register() every
        built-in and MCP tool already uses -- no new registration
        mechanism. require_approval=True: this is LLM-generated code, the
        same trust tier as shell/run_code/install_mcp_server, not a
        vetted built-in."""
        from .v2.tool_executor import Guardrails

        executor.register(
            tool_name,
            self.make_handler(code_path),
            guardrails=Guardrails(require_approval=True),
        )

    def load_forged_tools(self) -> List[Dict[str, Any]]:
        """Every previously-forged tool, so a fresh process can re-register
        them without re-running the LLM/sandbox pipeline again."""
        return [
            {"skill_id": sid, **entry}
            for sid, entry in self._registry.items()
            if entry.get("status") == "forged" and Path(entry.get("code_path", "")).exists()
        ]
