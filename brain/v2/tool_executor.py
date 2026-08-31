"""
Tool Executor — Modular tool dispatch with guardrails and validation.

Hermes-inspired architecture:
    - Each tool is a standalone async handler function.
    - ToolExecutor dispatches calls, enforces guardrails, validates results.
    - Mutation tools (create, send, write, delete) get automatic read-back
      verification: after the mutation, the corresponding read tool is called
      and the result is injected into the conversation.
    - On mutation failure, retry once before reporting error.

Legacy compatibility:
    - A ToolAdapter class wraps old dict-style tool definitions.
    - register_legacy(name, tool_dict, handler) for gradual migration.
"""

from __future__ import annotations

import asyncio
import ast
import json
import math
import operator as op
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# ToolResult — structured return from every tool
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Structured result from tool execution."""
    success: bool
    output: Any = None
    error: Optional[str] = None
    tool_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for LLM consumption.

        metadata is nested under its own key, not spread at the top level, so an
        unrelated future metadata key can never collide with "output"/"error".
        Only included when non-empty — most results carry none, and every extra
        key here is extra tokens sent back to the LLM every turn. This matters on
        both branches: success results can carry metadata too (e.g. execute()'s
        retry loop sets "attempts"/"recovered_after" on a result that succeeded
        after an initial failure), so dropping it only on the error branch would
        silently lose that signal.
        """
        base = (
            {"output": str(self.output) if self.output else ""}
            if self.success
            else {"error": self.error or "Unknown error"}
        )
        if self.metadata:
            base["metadata"] = self.metadata
        return base


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

@dataclass
class Guardrails:
    """Per-tool guardrail configuration."""
    allowed: bool = True
    require_approval: bool = False
    deny_patterns: List[str] = field(default_factory=list)
    allowed_patterns: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------

# Tools that mutate external state (need verification after execution)
MUTATION_TOOLS: Set[str] = {
    "calendar", "email", "remember", "write_file", "memory_save", "run_code",
    "forget",
}

# calendar and email dispatch both reads and writes through a single tool, so
# membership in MUTATION_TOOLS alone over-reports. These tools only mutate when
# their "action" parameter is one of the listed values; every other mutation
# tool has no action sub-parameter and always mutates.
ACTION_MUTATIONS: Dict[str, Set[str]] = {
    "calendar": {"create", "update", "delete"},
    "email": {"send"},
}

# run_code has no meaningful read-back: re-running it to verify would execute
# the side effects a second time. Its ToolResult already carries the subprocess
# exit status plus stdout/stderr, which is the verification signal.
NO_READBACK: Set[str] = {"run_code"}

# Mapping: mutation tool → read tool for verification.
#   read_params — static params for the read tool
#   carry       — {mutation_param: read_param} copied from the original call, so
#                 the read-back targets what was actually written
VERIFY_MAP: Dict[str, Dict[str, Any]] = {
    "calendar": {
        "read_tool": "calendar",
        "read_params": {"action": "agenda"},
        "description": "calendar agenda",
    },
    "email": {
        "read_tool": "email",
        "read_params": {"action": "triage"},
        "description": "email triage",
    },
    "remember": {
        "read_tool": "memory_search",
        "read_params": {"tier": "t4"},
        "carry": {"key": "query"},
        "description": "memory search T4",
    },
    "write_file": {
        "read_tool": "read_file",
        "read_params": {"limit": 20},
        "carry": {"path": "path"},
        "description": "read back written file",
    },
    "memory_save": {
        "read_tool": "memory_search",
        "read_params": {},
        "carry": {"tier": "tier", "title": "query"},
        "description": "memory search saved tier",
    },
    "forget": {
        "read_tool": "memory_search",
        "read_params": {"tier": "t4"},
        "carry": {"key_or_query": "query"},
        "description": "memory search T4 (should now come up empty)",
    },
}

# Total attempts for a mutating call: the first try plus LLM-corrected retries.
MAX_TOOL_ATTEMPTS = 3

# Commands that must never run, even once a human has approved the tool. This is
# a backstop against a typo or a misread instruction destroying data, not a
# security boundary — anything reaching `shell` can bypass a regex if it tries.
_DESTRUCTIVE_PATTERNS: List[str] = [
    r"\brm\s+-[a-z]*[rf]",           # rm -rf / rm -fr / rm -r
    r"\bdel\s+/[sq]",                 # del /s, del /q
    r"\bformat\s+[a-z]:",             # format c:
    r"\bRemove-Item\b[^\"]*-Recurse", # PowerShell recursive delete
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=",
    r":\s*\(\s*\)\s*\{.*\};\s*:",     # shell fork bomb
    r"\b(shutdown|Stop-Computer|Restart-Computer)\b",
    r"\bReset-ComputerMachinePassword\b",
    r">\s*/dev/sd[a-z]",
]

# Per-tool guardrails. Anything that can execute arbitrary code or launch a
# process requires explicit approval; approval is granted per exact tool+params
# call by putting that action's signature (see _action_signature) into
# context["approved_actions"].
TOOL_GUARDRAILS: Dict[str, Guardrails] = {
    "shell": Guardrails(
        require_approval=True,
        deny_patterns=list(_DESTRUCTIVE_PATTERNS),
    ),
    "run_code": Guardrails(
        require_approval=True,
        deny_patterns=list(_DESTRUCTIVE_PATTERNS),
    ),
    "open_app": Guardrails(require_approval=True),
}

# Handler type: async function(params, context) -> ToolResult
ToolHandler = Callable[[Dict[str, Any], Dict[str, Any]], Coroutine[Any, Any, ToolResult]]


class ToolExecutor:
    """
    Modular tool dispatch with guardrails and validation.

    Usage:
        executor = ToolExecutor()
        executor.register("time", handle_time)
        executor.register("calendar", handle_calendar, guardrails=Guardrails())
        result = await executor.execute("calendar", {"action": "agenda"}, context)
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, ToolHandler] = {}
        self._guardrails: Dict[str, Guardrails] = {}
        self._schemas: Dict[str, Dict[str, Any]] = {}
        self._validators: Dict[str, Callable[[ToolResult], bool]] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        guardrails: Optional[Guardrails] = None,
        validator: Optional[Callable[[ToolResult], bool]] = None,
        schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a tool handler."""
        self._handlers[name] = handler
        if guardrails is not None:
            self._guardrails[name] = guardrails
        if validator is not None:
            self._validators[name] = validator
        if schema is not None:
            self._schemas[name] = schema

    def register_legacy(
        self,
        name: str,
        tool_dict: Dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        """
        Register a legacy dict-style tool.

        tool_dict should have: {"description": str, "params": dict}
        """
        schema = _legacy_dict_to_schema(name, tool_dict)
        self.register(name, handler, schema=schema)

    @property
    def tool_names(self) -> List[str]:
        return list(self._handlers.keys())

    @property
    def schemas(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._schemas)

    def is_mutation(self, tool_name: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """Whether this specific call mutates external state.

        calendar/email serve reads and writes through one entry point, so their
        "action" parameter decides; all other mutation tools always mutate.
        """
        if tool_name not in MUTATION_TOOLS:
            return False
        mutating = ACTION_MUTATIONS.get(tool_name)
        if mutating is None:
            return True
        action = str((params or {}).get("action", "")).strip().lower()
        return action in mutating

    def check_guardrails(
        self,
        tool_name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolResult]:
        """Return a blocking ToolResult if guardrails reject this call, else None."""
        gr = self._guardrails.get(tool_name)
        if gr is None:
            return None

        if not gr.allowed:
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' is disabled by guardrails.",
                tool_name=tool_name,
            )

        # Match patterns against the serialized params so a dangerous string is
        # caught wherever it sits in the payload.
        probe = json.dumps(params, default=str) if params else ""

        for pattern in gr.deny_patterns:
            try:
                hit = re.search(pattern, probe, re.IGNORECASE)
            except re.error:
                continue
            if hit:
                return ToolResult(
                    success=False,
                    error=(
                        f"Blocked by guardrail: '{tool_name}' params matched "
                        f"denied pattern {pattern!r}."
                    ),
                    tool_name=tool_name,
                    metadata={"denied_by": pattern},
                )

        if gr.allowed_patterns:
            allowed = False
            for pattern in gr.allowed_patterns:
                try:
                    if re.search(pattern, probe, re.IGNORECASE):
                        allowed = True
                        break
                except re.error:
                    continue
            if not allowed:
                return ToolResult(
                    success=False,
                    error=(
                        f"Blocked by guardrail: '{tool_name}' params matched no "
                        f"allowed pattern."
                    ),
                    tool_name=tool_name,
                    metadata={"allowed_patterns": list(gr.allowed_patterns)},
                )

        if gr.require_approval:
            sig = _action_signature(tool_name, params)
            approved = set((context or {}).get("approved_actions") or ())
            if sig not in approved:
                return ToolResult(
                    success=False,
                    error=f"Tool '{tool_name}' requires explicit approval before running.",
                    tool_name=tool_name,
                    metadata={
                        "awaiting_approval": True,
                        "tool": tool_name,
                        "params": params,
                        "signature": sig,
                    },
                )

        return None

    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        allowed_tools: Optional[Set[str]] = None,
    ) -> ToolResult:
        """
        Execute a tool with guardrails, validation, and self-correcting retry.

        1. Reject unknown tools.
        1b. If allowed_tools is given, reject anything outside it — a hard
            deny independent of what the caller's own prompt offered, for
            callers (e.g. the post-turn memory-curation pass) that must not
            reach tools beyond a restricted set even if the model
            hallucinates a name that happens to be registered.
        2. Check guardrails (disabled / deny patterns / approval).
        3. Execute handler and validate the result.
        4. On a mutating call's failure, ask the LLM to correct the params from
           the error text and retry, up to MAX_TOOL_ATTEMPTS total. Every
           attempt is recorded in metadata["attempts"] so a final failure
           reports what was actually tried rather than only the last error.
        """
        ctx = context or {}
        call_params = dict(params or {})

        if tool_name not in self._handlers:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
                tool_name=tool_name,
            )

        if allowed_tools is not None and tool_name not in allowed_tools:
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' is not permitted in this context",
                tool_name=tool_name,
            )

        blocked = self.check_guardrails(tool_name, call_params, ctx)
        if blocked is not None:
            return blocked

        handler = self._handlers[tool_name]
        # Only mutating calls earn corrected retries — a failed read is not
        # fixed by different params, and retrying costs an extra LLM round-trip.
        max_attempts = MAX_TOOL_ATTEMPTS if self.is_mutation(tool_name, call_params) else 1

        attempts: List[Dict[str, Any]] = []
        result = ToolResult(success=False, error="Tool was never executed.", tool_name=tool_name)

        for attempt in range(max_attempts):
            result = await self._run_once(handler, tool_name, call_params, ctx)

            if result.success:
                if attempt:
                    result.metadata["attempts"] = attempts
                    result.metadata["recovered_after"] = attempt + 1
                result.tool_name = tool_name
                return result

            attempts.append({"params": dict(call_params), "error": result.error})

            if attempt == max_attempts - 1:
                break

            corrected = await self._correct_params(
                tool_name, call_params, result.error or "", ctx
            )
            if corrected is None:
                break

            # Corrected params must clear the same guardrails — an LLM must not
            # be able to talk its way past a deny pattern across a retry.
            blocked = self.check_guardrails(tool_name, corrected, ctx)
            if blocked is not None:
                blocked.metadata["attempts"] = attempts
                return blocked

            call_params = corrected

        result.tool_name = tool_name
        result.metadata["attempts"] = attempts
        if len(attempts) > 1:
            detail = "; ".join(
                f"attempt {i + 1}: {a['error']}" for i, a in enumerate(attempts)
            )
            result.error = f"Failed after {len(attempts)} attempts — {detail}"
        return result

    async def _run_once(
        self,
        handler: ToolHandler,
        tool_name: str,
        params: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> ToolResult:
        """Invoke a handler once and normalize whatever comes back."""
        try:
            result = await handler(params, ctx)
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Tool error: {e}",
                tool_name=tool_name,
            )

        if not isinstance(result, ToolResult):
            return ToolResult(
                success=False,
                error=(
                    f"Handler for '{tool_name}' returned "
                    f"{type(result).__name__}, expected ToolResult."
                ),
                tool_name=tool_name,
            )

        if not self._validate_result(tool_name, result):
            result.success = False
            if not result.error:
                result.error = "Tool returned invalid/unsuccessful result."
        return result

    async def _correct_params(
        self,
        tool_name: str,
        params: Dict[str, Any],
        error: str,
        ctx: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Ask the LLM for corrected params after a tool rejected these ones.

        Returns None when no router is available, the reply is unusable, or the
        proposal is identical to what already failed (retrying would be futile).
        """
        router = ctx.get("router")
        if router is None:
            return None

        schema = self._schemas.get(tool_name) or {}
        prompt = (
            "A tool call failed. Produce corrected parameters.\n\n"
            f"Tool: {tool_name}\n"
            f"Schema: {json.dumps(schema, default=str)[:600]}\n"
            f"Parameters tried: {json.dumps(params, default=str)[:400]}\n"
            f"Error: {error[:300]}\n\n"
            "Reply with ONLY a JSON object of corrected parameters. No prose. "
            "If no parameter change could fix this error, reply {}."
        )

        try:
            resp = await router.call(
                system_prompt="You repair malformed tool-call parameters. Output only JSON.",
                user_message=prompt,
                messages=[],
                max_tokens=300,
                temperature=0.0,
            )
        except Exception:
            return None

        corrected = _extract_json_object((getattr(resp, "text", None) or "").strip())
        if not isinstance(corrected, dict) or not corrected:
            return None
        if corrected == params:
            return None
        return corrected

    async def verify_mutation(
        self,
        tool_name: str,
        context: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolResult]:
        """
        After a successful mutation, call the corresponding read tool and return
        its result for verification.

        `params` is the original mutation's parameters; VERIFY_MAP's "carry"
        entries copy values across so the read-back targets what was written
        (e.g. read_file reads the same path write_file just wrote).
        """
        if tool_name not in VERIFY_MAP:
            return None

        vinfo = VERIFY_MAP[tool_name]
        read_tool = vinfo["read_tool"]
        if read_tool not in self._handlers:
            return None

        read_params = dict(vinfo["read_params"])
        for src, dest in (vinfo.get("carry") or {}).items():
            value = (params or {}).get(src)
            if value not in (None, ""):
                read_params[dest] = value

        try:
            return await self._handlers[read_tool](read_params, context or {})
        except Exception:
            return None

    def _validate_result(self, tool_name: str, result: ToolResult) -> bool:
        """Validate tool result. Uses custom validator if set, else checks success flag."""
        if tool_name in self._validators:
            return self._validators[tool_name](result)
        return result.success


# ---------------------------------------------------------------------------
# Legacy adapter
# ---------------------------------------------------------------------------

def _action_signature(tool_name: str, params: Dict[str, Any]) -> str:
    """A deterministic signature identifying one exact tool+params call.

    Used for "approve this exact action, not this tool forever" semantics — a
    signature is echoed back to the client in awaiting_approval.signature, and
    the client returns it verbatim in approved_actions to authorize a retry.
    Nothing needs to be recomputed client-side, which keeps this robust across
    languages/JSON serializers rather than requiring the client to replicate
    Python's exact canonicalization.
    """
    canonical = json.dumps(params or {}, sort_keys=True, default=str)
    return f"{tool_name}:{canonical}"


def _extract_json_object(text: str) -> Optional[Any]:
    """Pull the first balanced JSON object out of a possibly-chatty reply."""
    if not text:
        return None
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def _legacy_dict_to_schema(name: str, tool_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Convert old-style tool dict to JSON-Schema-like format."""
    params = tool_dict.get("params", {})
    properties = {}
    required = []
    for key, desc in params.items():
        properties[key] = {"type": "string", "description": str(desc)}
        # Heuristic: params with "for create/send" are optional
        if "optional" in str(desc).lower() or "for " in str(desc).lower():
            pass
        else:
            required.append(key)

    return {
        "name": name,
        "description": tool_dict.get("description", ""),
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# ---------------------------------------------------------------------------
# Built-in tool handlers
# ---------------------------------------------------------------------------

async def handle_time(params: Dict, ctx: Dict) -> ToolResult:
    """Get current date and time in this machine's own local timezone only."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")  # e.g. "+0530"
    offset_fmt = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC offset unknown"
    tz_name = now.tzname() or "local"
    return ToolResult(
        success=True,
        output=(
            now.strftime("%A, %B %d, %Y at %I:%M %p")
            + f" ({tz_name}, {offset_fmt}) — this is Master Sam's own device time; "
              "no other city's or timezone's current time is known."
        ),
    )


async def handle_chat(params: Dict, ctx: Dict) -> ToolResult:
    """Have a conversation or answer a question."""
    msg = params.get("message", "")
    if not msg:
        return ToolResult(success=True, output="Task completed.")
    # Delegate to memory-augmented chat if available
    memory = ctx.get("memory")
    bootstrap = ctx.get("bootstrap", {})
    router = ctx.get("router")
    if memory and router:
        # Build context-rich prompt
        sp = "You are Alfred, Master Sam's AI assistant.\n"
        try:
            t4 = memory.get_context_for_llm()
            if t4:
                sp += t4 + "\n"
        except Exception:
            pass
        for k in ["IDENTITY.md", "SOUL.md", "AGENTS.md"]:
            if k in bootstrap:
                sp += bootstrap[k] + "\n"
        # T3 episodic context
        try:
            r = memory.t3_find_episodes(msg, max_results=3)
            if r:
                parts = []
                for e in r:
                    try:
                        c = Path(e["path"]).read_text("utf-8")[:500]
                        parts.append(f"### {e['title']}\n{c}")
                    except Exception:
                        pass
                if parts:
                    sp += "## Relevant Past\n" + "\n\n".join(parts)
        except Exception:
            pass
        try:
            resp = await router.call(
                system_prompt=sp, user_message=msg,
                max_tokens=300, temperature=0.5,
            )
            return ToolResult(success=True, output=resp.text or "Done.")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    return ToolResult(success=True, output=msg or "Task completed.")


async def handle_calculator(params: Dict, ctx: Dict) -> ToolResult:
    """Evaluate a math expression safely using AST.

    Supports arithmetic plus an allowlist of math functions and constants, so
    real trigonometry/geometry questions work. Without them the LLM's correct
    first attempt (e.g. `47*tan(35*pi/180)` for an angle-of-elevation problem)
    failed with "Unsupported: Call", pushing it to fall back to run_code — which
    needs approval — or, worse, to answer from memory and hallucinate a number.
    """
    expression = params.get("expression", "0")
    safe_ops = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.Pow: op.pow,
        ast.USub: op.neg, ast.UAdd: op.pos,
        ast.FloorDiv: op.floordiv, ast.Mod: op.mod,
    }

    # Allowlist only: no attribute access, no builtins, no names beyond these.
    safe_funcs = {
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan,
        "atan2": math.atan2,
        "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
        "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
        "log": math.log, "log2": math.log2, "log10": math.log10, "exp": math.exp,
        "degrees": math.degrees, "radians": math.radians,
        "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
        "min": min, "max": max, "pow": pow,
        "hypot": math.hypot, "factorial": math.factorial,
    }
    safe_consts = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in safe_consts:
                return safe_consts[node.id]
            raise ValueError(f"Unknown name: {node.id}")
        if isinstance(node, ast.UnaryOp) and type(node.op) in safe_ops:
            return safe_ops[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in safe_ops:
            return safe_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.Call):
            # Only bare `name(...)` calls resolved against the allowlist —
            # ast.Attribute is never evaluated, so `math.__loader__` etc.
            # cannot be reached, and keyword/star args are refused outright.
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only direct function calls are allowed")
            fname = node.func.id
            if fname not in safe_funcs:
                raise ValueError(f"Unknown function: {fname}")
            if node.keywords:
                raise ValueError("Keyword arguments are not supported")
            args = [_eval(a) for a in node.args]
            return safe_funcs[fname](*args)
        raise ValueError(f"Unsupported: {type(node).__name__}")

    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree)
        if not isinstance(result, (int, float)) or isinstance(result, bool):
            return ToolResult(success=False, error="Calculator error: non-numeric result")
        # Trig on a calculator produces long floats (32.90862...); round for
        # readability but keep enough precision for "to the nearest tenth".
        if isinstance(result, float) and not result.is_integer():
            result = round(result, 6)
        return ToolResult(success=True, output=str(result))
    except Exception as e:
        return ToolResult(success=False, error=f"Calculator error: {e}")


async def handle_calendar(params: Dict, ctx: Dict) -> ToolResult:
    """List/create/delete Google Calendar events."""
    try:
        from ..tools.gws_client import GWSClient
        client = GWSClient()
    except ImportError as e:
        return ToolResult(success=False, error=f"GWS unavailable: {e}")

    act = params.get("action", "agenda")
    try:
        if act == "agenda":
            out = client.get_agenda(days=7)
        elif act == "list":
            out = client.get_agenda(days=14)
        elif act == "create":
            out = _calendar_create(client, params)
        elif act == "delete":
            query = params.get("summary") or params.get("query", "")
            if not query:
                return ToolResult(success=False, error="delete needs a summary/query naming the event")
            out = client.delete_event_by_query(query)
        else:
            out = client.get_agenda()

        if isinstance(out, str) and "error" in out.lower() and "Event created" not in out:
            return ToolResult(success=False, error=out[:500])
        return ToolResult(success=True, output=str(out)[:1000])
    except Exception as e:
        return ToolResult(success=False, error=f"Calendar error: {e}")


def _calendar_create(client: Any, params: Dict) -> str:
    """Handle calendar create with time parsing."""
    summary = params.get("summary", params.get("title", ""))
    start = params.get("start_time", "")
    end = params.get("end_time", "")
    desc = params.get("description", "")

    if not summary:
        return "Error: create needs a summary/title"

    # Try to extract time from summary
    sl = summary.lower()
    tm = re.search(r'(\d{1,2})\s*:\s*(\d{2})\s*(am|pm)?', sl)
    if not tm:
        tm = re.search(r'(\d{1,2})\s*(am|pm)', sl)

    if tm:
        hr = int(tm.group(1))
        mi = int(tm.group(2)) if tm.lastindex >= 2 and tm.group(2) and tm.group(2).isdigit() else 0
        if hr > 23 or mi > 59:
            return f"Error: Invalid time: {hr:02d}:{mi:02d}. Hours 0-23, minutes 0-59."
        sfx = tm.group(3) if tm.lastindex >= 3 else None
        if not sfx and tm.lastindex >= 2:
            g2 = tm.group(2)
            if g2 in ("am", "pm"):
                sfx, mi = g2, 0
        if sfx == "pm" and hr < 12:
            hr += 12
        elif sfx == "am" and hr == 12:
            hr = 0

        now = datetime.now()
        if "tomorrow" in sl:
            target = now + timedelta(days=1)
        else:
            day_map = {
                "monday": 0, "tuesday": 1, "wednesday": 2,
                "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            }
            target = now
            for day_name, day_num in day_map.items():
                if day_name in sl:
                    days_ahead = (day_num - now.weekday()) % 7 or 7
                    target = now + timedelta(days=days_ahead)
                    break

        dt = target.replace(hour=hr, minute=mi, second=0, microsecond=0)
        start = dt.strftime("%Y-%m-%dT%H:%M:%S")
    elif not start:
        return "Error: create needs start_time (ISO format)"

    # Validate start_time
    if start:
        try:
            datetime.fromisoformat(start)
        except Exception:
            return f"Error: Invalid start_time format: '{start}'. Use ISO like '2026-07-23T16:00:00'."

    if not end:
        try:
            dte = datetime.fromisoformat(start)
            end = (dte + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return "Error: create needs end_time"

    return client.create_event(summary=summary, start_time=start, end_time=end, description=desc)


async def handle_email(params: Dict, ctx: Dict) -> ToolResult:
    """Check/send Gmail."""
    try:
        from ..tools.gws_client import GWSClient
        client = GWSClient()
    except ImportError as e:
        return ToolResult(success=False, error=f"GWS unavailable: {e}")

    act = params.get("action", "triage")
    try:
        if act in ("triage", "list"):
            out = client.triage_emails()
        elif act == "read":
            eid = params.get("query", params.get("email_id", ""))
            if not eid:
                return ToolResult(success=False, error="read needs email_id")
            out = client.read_email(eid)
        elif act == "send":
            to = params.get("to", "")
            subj = params.get("subject", "")
            body = params.get("body", params.get("message", ""))
            if not to:
                return ToolResult(success=False, error="send needs 'to' email")
            out = client.send_email(to=to, subject=subj or "No Subject", body=body or "")
        else:
            out = client.triage_emails()

        if isinstance(out, str) and "error" in out.lower() and "Email sent" not in out:
            return ToolResult(success=False, error=out[:500])
        return ToolResult(success=True, output=str(out)[:1000])
    except Exception as e:
        return ToolResult(success=False, error=f"Email error: {e}")


async def handle_web_search(params: Dict, ctx: Dict) -> ToolResult:
    """Search the web via Exa or DuckDuckGo."""
    query = params.get("query", "")
    num = min(int(params.get("num_results", 5)), 10)
    if not query:
        return ToolResult(success=False, error="query required")

    # Clean query
    clean = re.sub(
        r'^(search|find|look up|search for|find info|look for)\s+',
        '', query, flags=re.I,
    )
    clean = re.sub(r'^(the web|the internet)\s+(for\s+)?', '', clean, flags=re.I)
    clean = re.sub(r'\s+(and\s+)?(save|write|fetch|download).*$', '', clean, flags=re.I)
    query = clean.strip()[:200] if clean.strip() else query

    # Try Exa first
    exa_key = os.environ.get("EXA_API_KEY", "")
    if exa_key:
        try:
            import requests as req
            r = req.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": exa_key, "Content-Type": "application/json"},
                json={"query": query, "numResults": min(num, 10),
                      "useAutoprompt": True, "text": True},
                timeout=15,
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    out = "\n".join(
                        f"{i+1}. {res.get('title', 'No title')}\n"
                        f"   {res.get('url', '')}\n"
                        f"   {res.get('text', '')[:200]}"
                        for i, res in enumerate(results)
                    )
                    return ToolResult(success=True, output=out)
        except Exception:
            pass

    # Fallback: DuckDuckGo
    try:
        import httpx
        from bs4 import BeautifulSoup
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
            resp = await client.post(url, data={"q": query})
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for el in soup.select(".result"):
                t = el.select_one(".result__title a")
                s = el.select_one(".result__snippet")
                if t:
                    results.append({
                        "title": t.get_text(strip=True),
                        "href": t.get("href", ""),
                        "body": s.get_text(strip=True) if s else "",
                    })
            if results:
                out = "\n".join(
                    f"{i+1}. {r['title']}\n   {r['href']}\n   {r['body'][:200]}"
                    for i, r in enumerate(results[:num])
                )
                return ToolResult(success=True, output=out)
        return ToolResult(success=True, output="No results found")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_web_fetch(params: Dict, ctx: Dict) -> ToolResult:
    """Read a webpage."""
    url = params.get("url", "")
    if not url or not url.startswith(("http://", "https://")):
        return ToolResult(success=False, error=f"Invalid URL: {url[:100]}")
    try:
        import httpx
        from bs4 import BeautifulSoup
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, follow_redirects=True)
            text = BeautifulSoup(r.text, "html.parser").get_text(separator="\n", strip=True)
            # 3000 chars was live-verified as this page.
            # Found stale during the 2026-08-27 tool audit: the page's own nav/
            # boilerplate had grown enough that the previously-verified content
            # now starts at index ~3039, just past the old cutoff -- Alfred
            # truthfully reported a cut-off page rather than guessing, but
            # couldn't answer. No fixed cap survives a page growing forever;
            # 6000 is a cheap, modest bump that covers today's real case (whole
            # page is 5581 chars) without ballooning context, not a guarantee
            # against a page that keeps growing.
            return ToolResult(success=True, output=text[:6000])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_shell(params: Dict, ctx: Dict) -> ToolResult:
    """Run a PowerShell command."""
    cmd = params.get("command", "")
    cwd = params.get("cwd")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        if r.returncode in (0, 1):
            out = (r.stdout or "")[:2000]
            if r.stderr:
                out += f"\nSTDERR: {r.stderr[:500]}"
            return ToolResult(success=True, output=out.strip())
        return ToolResult(success=False, error=r.stderr[:500])
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Command timed out (30s)")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_read_file(params: Dict, ctx: Dict) -> ToolResult:
    """Read a file from safe directories."""
    path = params.get("path", "")
    if not _is_safe_path(path):
        return ToolResult(success=False, error="Path not in safe directories")
    off = (params.get("offset", 1) or 1) - 1
    lim = params.get("limit", 2000) or 2000
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return ToolResult(success=True, output="".join(lines[off:off + lim])[:3000])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_write_file(params: Dict, ctx: Dict) -> ToolResult:
    """Write content to a file."""
    path = params.get("path", "")
    content = params.get("content", "")
    if not _is_safe_path(path):
        return ToolResult(success=False, error="Path not in safe directories")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(success=True, output=f"Written {len(content)} chars to {path}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_list_directory(params: Dict, ctx: Dict) -> ToolResult:
    """List files in a directory."""
    path = params.get("path", ".")
    if not _is_safe_path(path):
        return ToolResult(success=False, error="Path not in safe directories")
    try:
        entries = list(Path(path).iterdir())
        lines = [f"{'DIR' if e.is_dir() else 'FILE'} {e.name}" for e in entries[:50]]
        return ToolResult(success=True, output="\n".join(lines))
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_glob(params: Dict, ctx: Dict) -> ToolResult:
    """Find files by glob pattern, restricted to safe directories."""
    import glob as gl
    pattern = params.get("pattern", "**/*")

    # An absolute pattern must start inside a safe root; the wildcard portion is
    # stripped first because a glob metacharacter is not a real path component.
    anchor = pattern
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            anchor = pattern[:i]
            break
    if os.path.isabs(pattern) and not _is_safe_path(anchor or pattern):
        return ToolResult(success=False, error="Pattern not in safe directories")

    try:
        matches = gl.glob(pattern, recursive=True)
    except Exception as e:
        return ToolResult(success=False, error=str(e))

    # Relative patterns resolve against the cwd, and symlinks can escape either
    # way, so filter the results too rather than trusting the pattern alone.
    safe = [m for m in matches if _is_safe_path(m)]
    dropped = len(matches) - len(safe)
    out = "\n".join(safe[:50])
    if dropped:
        out += f"\n[{dropped} match(es) outside safe directories omitted]"
    return ToolResult(success=True, output=out.strip())


async def handle_screenshot(params: Dict, ctx: Dict) -> ToolResult:
    """Take a screenshot."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult(success=False, error="pyautogui not installed")
    ss_dir = Path(os.environ.get("TEMP", ".")) / "alfred_screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    p = ss_dir / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        pyautogui.screenshot(str(p))
        return ToolResult(success=True, output=f"Screenshot saved to {p}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_open_app(params: Dict, ctx: Dict) -> ToolResult:
    """Open a known desktop app.

    Never passes the raw request to a shell: with shell=True an unmapped name
    like "notepad & del *.*" would be executed verbatim by cmd.exe. Unmapped
    names must resolve to a real executable on PATH, and launch goes out with
    shell=False so metacharacters stay inert.
    """
    name = str(params.get("app_name", "")).strip().lower()
    if not name:
        return ToolResult(success=False, error="app_name required")

    am = {
        "calculator": "calc", "notepad": "notepad", "chrome": "chrome",
        "browser": "chrome", "explorer": "explorer",
        "settings": "ms-settings:", "terminal": "wt",
        "powershell": "powershell", "cmd": "cmd",
        "task manager": "taskmgr", "paint": "mspaint",
    }

    if name in am:
        target = am[name]
    else:
        if any(c in name for c in ';&|<>$`\n"\''):
            return ToolResult(
                success=False,
                error=f"Refusing to open '{name}': unsupported characters in app name.",
            )
        resolved = shutil.which(name)
        if not resolved:
            known = ", ".join(sorted(am))
            return ToolResult(
                success=False,
                error=f"Unknown app '{name}'. Known apps: {known}.",
            )
        target = resolved

    try:
        if target.endswith(":"):
            # A URI handler (ms-settings:) is not an executable; it needs the
            # OS shell resolver. Only reachable for fixed values in the map.
            os.startfile(target)
        else:
            subprocess.Popen([target], shell=False)
        return ToolResult(success=True, output=f"Opened {name}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_gws(params: Dict, ctx: Dict) -> ToolResult:
    """Run Google Workspace commands beyond calendar/email."""
    try:
        from ..tools.gws_client import GWSClient
    except ImportError as e:
        return ToolResult(success=False, error=f"GWS unavailable: {e}")
    cmd = params.get("command", "")
    if not cmd:
        return ToolResult(success=False, error="No command")
    try:
        out = GWSClient().run_generic(cmd)
        if "error" in out.lower() and "not yet wrapped" not in out.lower():
            return ToolResult(success=False, error=out[:500])
        return ToolResult(success=True, output=out[:1000])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_remember(params: Dict, ctx: Dict) -> ToolResult:
    """Save a fact to long-term profile (T4)."""
    k = params.get("key", "")
    v = params.get("value", "")
    s = params.get("section", "Preferences")
    if not k or not v:
        return ToolResult(success=False, error="key and value required")
    memory = ctx.get("memory")
    if memory:
        try:
            memory.t4_set(k.strip(), v.strip(), s.strip())
            return ToolResult(success=True, output=f"Saved: {k} = {v}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    return ToolResult(success=False, error="Memory system unavailable")


async def handle_memory_save(params: Dict, ctx: Dict) -> ToolResult:
    """Save to memory (T3/T4/T5)."""
    memory = ctx.get("memory")
    if not memory:
        return ToolResult(success=False, error="Memory system unavailable")
    tier = params.get("tier", "t4").lower()
    content = params.get("content", "")
    title = params.get("title", "")
    if not content:
        return ToolResult(success=False, error="content required")
    try:
        if tier == "t3":
            p = memory.t3_save_episode(title or content[:50], content)
            return ToolResult(success=True, output=f"Saved T3: {p}")
        elif tier == "t4":
            memory.t4_set(title or "saved_note", content)
            return ToolResult(success=True, output="Saved T4")
        elif tier == "t5":
            memory.t5_append(content, title or None)
            return ToolResult(success=True, output="Saved T5")
        return ToolResult(success=False, error=f"Unknown tier: {tier}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_memory_search(params: Dict, ctx: Dict) -> ToolResult:
    """Search memory tiers."""
    memory = ctx.get("memory")
    if not memory:
        return ToolResult(success=False, error="Memory system unavailable")
    q = params.get("query", "")
    tier = params.get("tier", "all").lower()
    if not q:
        return ToolResult(success=False, error="query required")
    out = []
    try:
        if tier in ("t3", "all"):
            for r in memory.t3_find_episodes(q, max_results=3):
                out.append(f"[T3] {r['title']} (score:{r['final_score']:.2f})")
        if tier in ("t4", "all"):
            # t4_get is an exact key-name lookup, so a natural-language query
            # ("what do you know about my doubt session") will not match a
            # stored key ("physics_doubt_session"). t4_search handles that:
            # exact match, then keyword overlap, then semantic fallback.
            for k, val in memory.t4_search(q):
                out.append(f"[T4] {k}: {val}")
        if tier in ("t5", "all"):
            for r in memory.t5_search(q, max_results=3):
                out.append(f"[T5] {r['title']}: {r['snippet']}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))
    if not out:
        return ToolResult(success=True, output=f"No memories for '{q}'")
    return ToolResult(success=True, output="\n".join(out))


async def handle_forget(params: Dict, ctx: Dict) -> ToolResult:
    """Delete a fact from the long-term profile (T4)."""
    memory = ctx.get("memory")
    if not memory:
        return ToolResult(success=False, error="Memory system unavailable")
    key_or_query = params.get("key_or_query", "")
    if not key_or_query:
        return ToolResult(success=False, error="key_or_query required")
    try:
        out = memory.t4_forget(key_or_query.strip())
    except Exception as e:
        return ToolResult(success=False, error=str(e))
    # "No stored fact found" / an ambiguous-match listing are informational,
    # not failures -- same convention as delete_event_by_query's calendar
    # equivalent (gws_client.py).
    return ToolResult(success=True, output=out)


async def handle_weather(params: Dict, ctx: Dict) -> ToolResult:
    """Get weather for a location."""
    loc = params.get("location", "auto")
    try:
        import requests
        r = requests.get(
            f"https://wttr.in/{loc}?format=%l:+%C+%t,+%h",
            timeout=10,
            headers={"User-Agent": "Alfred/1.0"},
        )
        if r.status_code == 200:
            return ToolResult(success=True, output=r.text.strip())
        return ToolResult(success=False, error=f"Weather API: {r.status_code}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_run_code(params: Dict, ctx: Dict) -> ToolResult:
    """Execute Python or PowerShell safely."""
    code = params.get("code", "")
    lang = params.get("language", "python").lower()
    if not code:
        return ToolResult(success=False, error="code required")

    if lang == "shell":
        try:
            r = subprocess.run(
                ["powershell", "-Command", code],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
            out = r.stdout[:2000]
            if r.stderr:
                out += f"\nSTDERR: {r.stderr[:500]}"
            if r.returncode != 0:
                return ToolResult(success=False, error=f"Exit {r.returncode}: {out[:500]}")
            return ToolResult(success=True, output=out.strip())
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Code execution timed out (60s)")
    else:
        wrapped = textwrap.dedent(f"""\
import sys,json,subprocess,os,re,math,random,datetime,pathlib
from pathlib import Path
try:
{textwrap.indent(code, "    ")}
except Exception as e:
    print(f"ERROR: {{e}}",file=sys.stderr); sys.exit(1)""")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(wrapped)
                tmp = f.name
            r = subprocess.run(
                [sys.executable, tmp],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
            )
            out = r.stdout[:2000]
            if r.stderr:
                err = r.stderr[:500]
                if "ERROR:" in err:
                    return ToolResult(success=False, error=err.split("ERROR:")[-1].strip())
            if r.returncode != 0:
                return ToolResult(success=False, error=f"Exit {r.returncode}: {r.stderr[:300]}")
            return ToolResult(success=True, output=out.strip())
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Code execution timed out (60s)")
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_roots() -> List[Path]:
    """Directories the file tools are allowed to touch."""
    home = Path.home()
    roots = [home / d for d in ("Coding", "Documents", "Downloads", "Desktop")]
    roots.append(home)
    roots.append(Path(r"C:\Coding"))
    resolved: List[Path] = []
    for r in roots:
        try:
            rp = r.resolve()
        except Exception:
            continue
        if rp.exists() and rp not in resolved:
            resolved.append(rp)
    return resolved


def _is_safe_path(path: str) -> bool:
    """Check whether a path resolves inside an allowed directory.

    Uses path-component containment rather than a string prefix: a raw
    startswith() check would let "C:\\Coding-evil" pass as "C:\\Coding".
    """
    if not path:
        return False
    try:
        target = Path(path).resolve()
    except Exception:
        return False
    for root in _safe_roots():
        if target == root:
            return True
        try:
            if target.is_relative_to(root):
                return True
        except AttributeError:  # Python < 3.9
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Factory: create a fully-registered ToolExecutor
# ---------------------------------------------------------------------------

def create_tool_executor() -> ToolExecutor:
    """
    Create a ToolExecutor with all built-in tools registered.

    Returns a ready-to-use executor.
    """
    executor = ToolExecutor()

    # Register all built-in handlers
    builtin_tools: Dict[str, ToolHandler] = {
        "time": handle_time,
        "chat": handle_chat,
        "calculator": handle_calculator,
        "calendar": handle_calendar,
        "email": handle_email,
        "web_search": handle_web_search,
        "web_fetch": handle_web_fetch,
        "shell": handle_shell,
        "read_file": handle_read_file,
        "write_file": handle_write_file,
        "list_directory": handle_list_directory,
        "glob": handle_glob,
        "screenshot": handle_screenshot,
        "open_app": handle_open_app,
        "gws": handle_gws,
        "remember": handle_remember,
        "memory_save": handle_memory_save,
        "memory_search": handle_memory_search,
        "forget": handle_forget,
        "weather": handle_weather,
        "run_code": handle_run_code,
    }

    for name, handler in builtin_tools.items():
        executor.register(name, handler, guardrails=TOOL_GUARDRAILS.get(name))

    return executor


if __name__ == "__main__":
    import asyncio

    async def _demo() -> None:
        ex = create_tool_executor()
        print(f"Registered {len(ex.tool_names)} tools\n")

        r = await ex.execute("time", {}, {})
        print(f"time            -> success={r.success} output={str(r.output)[:48]!r}")

        r = await ex.execute("calculator", {"expression": "17*23"}, {})
        print(f"calculator      -> success={r.success} output={r.output!r}")

        r = await ex.execute("nope", {}, {})
        print(f"unknown tool    -> success={r.success} error={r.error!r}")

        r = await ex.execute("shell", {"command": "echo hi"}, {})
        print(f"shell (no appr) -> success={r.success} awaiting={r.metadata.get('awaiting_approval')}")

        denied_params = {"command": "rm -rf /"}
        denied_sig = _action_signature("shell", denied_params)
        r = await ex.execute("shell", denied_params, {"approved_actions": {denied_sig}})
        print(f"shell (denied)  -> success={r.success} error={str(r.error)[:60]!r}")

        print(f"\ncalendar agenda is mutation? {ex.is_mutation('calendar', {'action': 'agenda'})}")
        print(f"calendar create is mutation? {ex.is_mutation('calendar', {'action': 'create'})}")

    asyncio.run(_demo())
