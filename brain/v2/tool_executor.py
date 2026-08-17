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
import operator as op
import os
import re
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
        """Convert to dict for LLM consumption."""
        if self.success:
            return {"output": str(self.output) if self.output else ""}
        return {"error": self.error or "Unknown error"}


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
    "calendar", "email", "remember", "set_reminder",
    "delete_reminder", "write_file", "memory_save", "run_code",
}

# Mapping: mutation tool → read tool for verification
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
    "set_reminder": {
        "read_tool": "list_reminders",
        "read_params": {},
        "description": "list reminders",
    },
    "remember": {
        "read_tool": "memory_search",
        "read_params": {"tier": "t4"},
        "description": "memory search T4",
    },
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

    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        """
        Execute a tool with guardrails and validation.

        1. Check guardrails.
        2. Execute handler.
        3. Validate result (custom validator or ToolResult.success).
        4. If mutation failed, retry once.
        """
        ctx = context or {}

        # --- Unknown tool ---
        if tool_name not in self._handlers:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
                tool_name=tool_name,
            )

        # --- Guardrail check ---
        gr = self._guardrails.get(tool_name)
        if gr and not gr.allowed:
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' is disabled by guardrails.",
                tool_name=tool_name,
            )

        # --- Execute ---
        handler = self._handlers[tool_name]
        try:
            result = await handler(params, ctx)
        except Exception as e:
            result = ToolResult(
                success=False,
                error=f"Tool error: {e}",
                tool_name=tool_name,
            )

        # --- Validate ---
        if not self._validate_result(tool_name, result):
            result.success = False
            if not result.error:
                result.error = "Tool returned invalid/unsuccessful result."

        # --- Retry on mutation failure ---
        if not result.success and tool_name in MUTATION_TOOLS:
            try:
                result = await handler(params, ctx)
                if not self._validate_result(tool_name, result):
                    result.success = False
                    if not result.error:
                        result.error = "Tool failed on retry."
            except Exception as e:
                result = ToolResult(
                    success=False,
                    error=f"Tool error on retry: {e}",
                    tool_name=tool_name,
                )

        result.tool_name = tool_name
        return result

    async def verify_mutation(
        self,
        tool_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolResult]:
        """
        After a successful mutation, call the corresponding read tool
        and return its result for LLM verification.
        """
        if tool_name not in VERIFY_MAP:
            return None

        vinfo = VERIFY_MAP[tool_name]
        read_tool = vinfo["read_tool"]
        read_params = dict(vinfo["read_params"])

        if read_tool not in self._handlers:
            return None

        try:
            handler = self._handlers[read_tool]
            return await handler(read_params, context or {})
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
    """Get current date and time."""
    now = datetime.now()
    return ToolResult(
        success=True,
        output=now.strftime("%A, %B %d, %Y at %I:%M %p"),
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
    """Evaluate a math expression safely using AST."""
    expression = params.get("expression", "0")
    safe_ops = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.Pow: op.pow,
        ast.USub: op.neg, ast.FloorDiv: op.floordiv, ast.Mod: op.mod,
    }

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in safe_ops:
            return safe_ops[type(node.op)](0, _eval(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in safe_ops:
            return safe_ops[type(node.op)](_eval(node.left), _eval(node.right))
        raise ValueError(f"Unsupported: {type(node).__name__}")

    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree)
        return ToolResult(
            success=True,
            output=str(result) if isinstance(result, (int, float)) else "Invalid",
        )
    except Exception as e:
        return ToolResult(success=False, error=f"Calculator error: {e}")


async def handle_calendar(params: Dict, ctx: Dict) -> ToolResult:
    """List/create Google Calendar events."""
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
            return ToolResult(success=True, output=text[:3000])
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
    """Find files by glob pattern."""
    import glob as gl
    pattern = params.get("pattern", "**/*")
    matches = gl.glob(pattern, recursive=True)
    return ToolResult(success=True, output="\n".join(matches[:50]))


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
    """Open a desktop app."""
    name = params.get("app_name", "").lower()
    am = {
        "calculator": "calc", "notepad": "notepad", "chrome": "chrome",
        "browser": "chrome", "explorer": "explorer",
        "settings": "ms-settings:", "terminal": "wt",
        "powershell": "powershell", "cmd": "cmd",
        "task manager": "taskmgr", "paint": "mspaint",
    }
    exe = am.get(name, name)
    try:
        subprocess.Popen([exe], shell=True)
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


async def handle_set_reminder(params: Dict, ctx: Dict) -> ToolResult:
    """Set a reminder."""
    text = params.get("text", "")
    when = params.get("when", "")
    cat = params.get("category", "general")
    if not text or not when:
        return ToolResult(success=False, error="text and when required")
    db = ctx.get("db")
    if db:
        try:
            rid = db.add_reminder(text, when, cat)
            return ToolResult(success=True, output=f"Reminder set: '{text}' at {when} (ID: {rid})")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    return ToolResult(success=False, error="Database unavailable")


async def handle_list_reminders(params: Dict, ctx: Dict) -> ToolResult:
    """List pending reminders."""
    db = ctx.get("db")
    if not db:
        return ToolResult(success=False, error="Database unavailable")
    inc = params.get("include_fired", "").lower() == "true"
    try:
        rems = db.list_reminders(inc)
        if not rems:
            return ToolResult(success=True, output="No reminders.")
        lines = [f"- ID {r['id']}: {r['text']} (due: {r['due_at']})" for r in rems]
        return ToolResult(success=True, output="\n".join(lines))
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def handle_delete_reminder(params: Dict, ctx: Dict) -> ToolResult:
    """Delete a reminder by ID."""
    db = ctx.get("db")
    if not db:
        return ToolResult(success=False, error="Database unavailable")
    try:
        rid = int(params.get("id", 0))
        if db.delete_reminder(rid):
            return ToolResult(success=True, output=f"Reminder {rid} deleted.")
        return ToolResult(success=False, error=f"Reminder {rid} not found.")
    except (ValueError, TypeError):
        return ToolResult(success=False, error="Invalid ID")


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
            v = memory.t4_get(q)
            if v:
                out.append(f"[T4] {q}: {v}")
        if tier in ("t5", "all"):
            for r in memory.t5_search(q, max_results=3):
                out.append(f"[T5] {r['title']}: {r['snippet']}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))
    if not out:
        return ToolResult(success=True, output=f"No memories for '{q}'")
    return ToolResult(success=True, output="\n".join(out))


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

def _is_safe_path(path: str) -> bool:
    """Check if a file path is within allowed directories."""
    try:
        ap = os.path.realpath(path)
    except Exception:
        return False
    home = str(Path.home())
    safe = [os.path.join(home, d) for d in ["Coding", "Documents", "Downloads", "Desktop"]]
    safe.append(home)
    coding_raw = os.path.realpath(r"C:\Coding")
    if coding_raw not in safe:
        safe.append(coding_raw)
    return any(ap.startswith(d) for d in safe if os.path.exists(d))


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
        "set_reminder": handle_set_reminder,
        "list_reminders": handle_list_reminders,
        "delete_reminder": handle_delete_reminder,
        "memory_save": handle_memory_save,
        "memory_search": handle_memory_search,
        "weather": handle_weather,
        "run_code": handle_run_code,
    }

    for name, handler in builtin_tools.items():
        executor.register(name, handler)

    return executor
