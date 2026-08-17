"""
Alfred - Unified Brain with LLM-driven 4-phase loop.

Merges v1 (kernel tools, LLM) with v2 (4-phase loop, 5-tier memory).
"""

import os
import sys
import json
import re
import time
import asyncio
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from dotenv import load_dotenv
from brain.llm_router import LLMRouter, LLMResponse

load_dotenv(Path(__file__).parent.parent / ".env")

from .memory.five_tier import get_memory  # noqa: E402
from .memory.skill_manager import get_skill_manager, Skill  # noqa: E402
from .local_db import get_local_db  # noqa: E402
from .goal_inference import get_goal_expander  # noqa: E402
from .errors import get_error_classifier  # noqa: E402
from .recovery import get_recovery_manager  # noqa: E402
from .intent_classifier import IntentClassifier  # noqa: E402
from .tools.gws_client import GWSClient  # noqa: E402


class Phase(Enum):
    PLANNING = "planning"
    EXECUTION = "execution"
    SELF_CORRECTION = "self_correction"
    REFLECTION = "reflection"


@dataclass
class ExecutionStep:
    step_number: int = 0
    description: str = ""
    tool: str = ""
    params: Dict = field(default_factory=dict)
    result: str = ""
    error: str = ""


@dataclass
class ToolResult:
    tool: str
    params: Dict
    result: str
    error: str = ""


@dataclass
class TaskContext:
    original_task: str
    current_phase: Phase = Phase.PLANNING
    skill: Optional[Skill] = None
    plan: List[ExecutionStep] = field(default_factory=list)
    execution_results: List[ToolResult] = field(default_factory=list)
    corrections_made: int = 0
    final_response: str = ""
    tool_calls_made: List[str] = field(default_factory=list)
    tried_approaches: List[Dict] = field(default_factory=list)

    def add_step(self, step: ExecutionStep):
        self.plan.append(step)
        step.step_number = len(self.plan)

    def add_result(self, result: ToolResult):
        self.execution_results.append(result)
        if result.error:
            self.corrections_made += 1


class Alfred:
    """
    Alfred's unified brain: 4-phase loop with LLM planning and real tools.

    Phase 0: Goal Inference - expand user intent via LLM
    Phase 1: Planning - LLM generates execution plan using available tools
    Phase 2: Execution - run tools, collect results
    Phase 3: Self-Correction - classify errors, search web for solutions, retry
    Phase 4: Reflection - save to memory, generate skills
    """

    FORMATTER_MODEL = "llama-3.1-8b-instant"
    PLANNER_MODEL = "llama-3.1-8b-instant"
    CHAT_MODEL = "llama-3.1-8b-instant"

    # Bootstrap files path
    BOOTSTRAP_DIR = Path(
        os.environ.get("OBSIDIAN_VAULT_PATH", r"C:\Coding\notes idk obsidian\Aflred-brain")
    )

    def __init__(self):
        self.memory = get_memory()
        self.skill_manager = get_skill_manager()
        self.goal_expander = get_goal_expander()
        self.db = get_local_db()
        self.goal_inference_enabled = self.goal_expander.enabled
        self.error_classifier = get_error_classifier()
        self.recovery_manager = get_recovery_manager()

        # Groq client
        self._groq_client = None
        self._groq_key = os.environ.get("GROQ_API_KEY", "")
        if not self._groq_key:
            print("[Alfred] GROQ_API_KEY NOT SET - LLM features disabled!", flush=True)
        else:
            print(f"[Alfred] GROQ_API_KEY loaded ({self._groq_key[:10]}...)", flush=True)

        # Gemini client
        self._gemini_client = None
        self._google_key = os.environ.get("GOOGLE_API_KEY", "")
        if self._google_key:
            print(f"[Alfred] GOOGLE_API_KEY loaded ({self._google_key[:10]}...)", flush=True)

        # OpenRouter client
        self._openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._openrouter_client = None
        if self._openrouter_key:
            print(f"[Alfred] OPENROUTER_API_KEY loaded ({self._openrouter_key[:10]}...)", flush=True)

        # LLM Router — unified fallback chain with circuit breakers
        self._router = LLMRouter(
            groq_key=self._groq_key,
            gemini_key=self._google_key,
            openrouter_key=self._openrouter_key,
        )
        print(f"[Alfred] LLM Router initialized with {len(self._router.providers)} providers", flush=True)

        # Intent classifier
        self.intent_classifier = IntentClassifier(groq_api_key=self._groq_key)

        # Load bootstrap files
        self._bootstrap_context = self._load_bootstrap()

        # Heartbeat
        self.heartbeat_enabled = True
        self.heartbeat_interval = 7200
        self._heartbeat_task = None
        self._pending_alerts: List[Dict] = []

        # Register tools
        self._tools: Dict[str, Dict] = {}
        self._register_tools()

        # Start heartbeat
        self._start_heartbeat()

    def _load_bootstrap(self) -> Dict[str, str]:
        """Load bootstrap files from Obsidian vault.
        These are injected into every LLM planning call for improvisation context."""
        bootstrap = {}
        for name in ["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md"]:
            path = self.BOOTSTRAP_DIR / name
            try:
                if path.exists():
                    bootstrap[name] = path.read_text(encoding="utf-8")
                    print(f"[Alfred] Bootstrap loaded: {name} ({len(bootstrap[name])} chars)")
                else:
                    print(f"[Alfred] Bootstrap missing: {name}")
            except Exception as e:
                print(f"[Alfred] Failed to load {name}: {e}")
        return bootstrap

    def _get_bootstrap_prompt(self, t3_context: str = None) -> str:
        """Format bootstrap context for LLM system prompt.

        T4 user profile is placed FIRST so the LLM always sees it.
        """
        parts = []

        # T4 user profile — ALWAYS first, with explicit instruction
        try:
            t4_context = self.memory.get_context_for_llm()
            if t4_context and "User Profile" in t4_context:
                parts.append(
                    "## MASTER SAM — KNOWN FACTS (USE THESE to answer personal questions)\n"
                    + t4_context.split("## User Profile:")[-1].strip()
                )
        except Exception as e:
            print(f"[Alfred] T4 context error: {e}")

        if "IDENTITY.md" in self._bootstrap_context:
            parts.append("## WHO YOU ARE\n" + self._bootstrap_context["IDENTITY.md"])
        if "SOUL.md" in self._bootstrap_context:
            parts.append("## YOUR PERSONALITY\n" + self._bootstrap_context["SOUL.md"])
        if "AGENTS.md" in self._bootstrap_context:
            parts.append("## OPERATING INSTRUCTIONS\n" + self._bootstrap_context["AGENTS.md"])

        # Add T3 episodic memory context if provided
        if t3_context:
            parts.append("## RELEVANT PAST EXPERIENCES\n" + t3_context[:1000])

        return "\n\n".join(parts)

    def _get_groq_client(self):
        """Lazy init Groq client."""
        if self._groq_client is None and self._groq_key:
            from groq import Groq
            self._groq_client = Groq(api_key=self._groq_key, timeout=10.0)
        return self._groq_client

    def _register_tools(self):
        """Register all available tools with descriptions for LLM planning."""
        self._tools = {
            "chat": {
                "description": "General conversation or answer a question. Use for any task that doesn't fit other tools.",
                "params": {"message": "The message or question to respond to"},
            },
            "calculator": {
                "description": "Evaluate a mathematical expression. Use for any math calculation.",
                "params": {"expression": "Math expression like '2+2' or '15*7'"},
            },
            "calendar": {
                "description": "Check Google Calendar agenda, list upcoming events, or create new events. For create: summary (title), start_time (ISO: YYYY-MM-DD HH:MM:SS), end_time (ISO). Actions: agenda (default), list, create.",
                "params": {"action": "agenda, list, or create", "summary": "Event title (for create)", "start_time": "ISO datetime like 2026-05-22 15:00:00 (for create)", "end_time": "ISO datetime (for create, auto +1hr if omitted)"},
            },
            "email": {
                "description": "Check Gmail inbox, triage emails, read specific email, or send emails. For send: to (email address), subject, body. Actions: triage (default), list, read, send.",
                "params": {"action": "triage, list, read, or send", "to": "Recipient email (for send)", "subject": "Email subject (for send)", "body": "Email body text (for send)"},
            },
            "web_search": {
                "description": "Search the web for information. Use Exa API with DuckDuckGo fallback.",
                "params": {"query": "Search query", "num_results": "Number of results (default 5)"},
            },
            "web_fetch": {
                "description": "Fetch and read the content of a webpage. Use after web_search to read a specific URL.",
                "params": {"url": "The URL to fetch and read"},
            },
            "shell": {
                "description": "Execute a shell/PowerShell command. Use for system operations, file management, running scripts.",
                "params": {"command": "Shell command to execute", "cwd": "Working directory (optional)"},
            },
            "read_file": {
                "description": "Read the contents of a file. Safe directories only.",
                "params": {"path": "Path to the file", "offset": "Start line (optional)", "limit": "Max lines (optional)"},
            },
            "write_file": {
                "description": "Write content to a file. Creates the file if it doesn't exist.",
                "params": {"path": "Path to the file", "content": "Content to write"},
            },
            "list_directory": {
                "description": "List files in a directory.",
                "params": {"path": "Directory path to list"},
            },
            "glob": {
                "description": "Find files matching a glob pattern.",
                "params": {"pattern": "Glob pattern like '**/*.py'"},
            },
            "screenshot": {
                "description": "Take a screenshot of the current screen.",
                "params": {},
            },
            "gws": {
                "description": "Run any Google Workspace command. Covers Drive, Gmail, Calendar, Sheets, Docs. Use when calendar/email tools aren't enough. Examples: 'drive list', 'drive search tax', 'email triage'.",
                "params": {"command": "Command like 'drive list' or 'drive search tax documents'"},
            },
            "open_app": {
                "description": "Open/launch a desktop application by name.",
                "params": {"app_name": "Application name like 'calculator', 'notepad', 'chrome'"},
            },
            "time": {
                "description": "Get the current date and time. Use for ANY question about time, date, day of week, or current moment.",
                "params": {},
            },
            "remember": {
                "description": "Save a piece of information about the user to their profile (T4 memory). Use when the user says 'remember', 'save this', 'note that', or shares personal preferences. Data persists across sessions.",
                "params": {"key": "What to call this information (e.g. 'favorite_food', 'location', 'preferred_name')", "value": "The value to remember", "section": "Optional category (default: 'Preferences')"},
            },
            "set_reminder": {
                "description": "Set a reminder that will notify the user at a specified time. Use when asked to 'remind me', 'set a reminder', 'alert me', 'notify me'.",
                "params": {"text": "What to remind about", "when": "When to remind (ISO datetime like '2026-05-19 15:30:00' or natural language)", "category": "Optional category (default: 'general')"},
            },
            "list_reminders": {
                "description": "List all pending reminders.",
                "params": {"include_fired": "Set to 'true' to include completed reminders (default: 'false')"},
            },
            "delete_reminder": {
                "description": "Delete a reminder by its ID.",
                "params": {"id": "The reminder ID to delete"},
            },
            "memory_save": {
                "description": "Save information to Alfred's memory system. Can save to T3 (episodic memory - past events), T4 (user profile - preferences/facts), or T5 (archive - permanent storage). Use when asked to 'remember', 'save to memory', 'store this', 'archive'.",
                "params": {"tier": "Which memory tier: 't3' (episodic), 't4' (profile), or 't5' (archive)", "content": "The content to save", "title": "Optional title for the memory"},
            },
            "memory_search": {
                "description": "Search Alfred's memory system across all tiers. Searches T3 (episodic), T4 (profile), and T5 (archive) for relevant information. Use when asked 'do you remember', 'search memory', 'what do you know about', 'recall'.",
                "params": {"query": "What to search for", "tier": "Optional: 't3', 't4', 't5', or 'all' (default: 'all')"},
            },
            "weather": {
                "description": "Get current weather information. Use for weather, temperature, forecast queries. Uses wttr.in API.",
                "params": {"location": "Location name (default: auto-detect)"},
            },
            "run_code": {
                "description": "Execute Python code or PowerShell commands. Use for ANY task that needs custom logic - data processing, file operations, API calls, installing packages, modifying files. Has full access to the filesystem and network. THIS is your main tool for open-ended problem-solving.",
                "params": {"code": "Python code or PowerShell command to execute", "language": "'python' or 'shell' (default: 'python')"},
            },
        }

    def _get_tool_descriptions(self) -> str:
        """Get formatted tool descriptions for LLM prompt."""
        lines = ["Available tools:"]
        for name, info in self._tools.items():
            lines.append(f"- **{name}**: {info['description']}")
            if info["params"]:
                params_str = ", ".join(f'"{k}": <value>' for k in info["params"])
                lines.append(f"  Params: {{{params_str}}}")
        return "\n".join(lines)

    def _start_heartbeat(self):
        """Start the heartbeat background task."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._heartbeat_task = loop.create_task(self._heartbeat_loop())
                print(f"[Alfred] Heartbeat started (interval: {self.heartbeat_interval}s)")
        except Exception as e:
            print(f"[Alfred] Heartbeat init failed: {e}")

    async def _heartbeat_loop(self):
        """Main heartbeat loop - runs every 30s. Heavy tasks (email+calendar) run every 7200s (2h)."""
        heavy_interval = 7200
        last_heavy_run = time.monotonic()
        while self.heartbeat_enabled:
            await asyncio.sleep(30)
            try:
                self.check_due_reminders()
                await self._check_scheduled_tasks()
                if time.monotonic() - last_heavy_run >= heavy_interval:
                    last_heavy_run = time.monotonic()
                    await self._execute_heartbeat()
            except Exception as e:
                print(f"[Alfred] Heartbeat error: {e}")

    def check_due_reminders(self) -> List[Dict]:
        """Check for due reminders and return alerts."""
        try:
            due = self.db.get_due_reminders()
            alerts = []
            for r in due:
                self.db.mark_reminder_fired(r["id"])
                alert = {"type": "reminder", "text": r["text"], "id": r["id"], "category": r.get("category", "general")}
                alerts.append(alert)
                self._pending_alerts.append(alert)
                print(f"[Alfred] Reminder fired: {r['text']}")
            return alerts
        except Exception as e:
            print(f"[Alfred] Reminder check failed: {e}")
            return []

    async def _check_scheduled_tasks(self):
        """Check and execute due cron tasks."""
        try:
            from .local_db import get_local_db
            due = get_local_db().get_due_scheduled_tasks()
            for task in due:
                print(f"[Alfred] Cron task due: {task['task']}")
                result = await self.execute(task["task"])
                get_local_db().update_last_run(task["id"])
                output = result.get("output", "") if result else ""
                if output:
                    self._pending_alerts.append({
                        "type": "cron",
                        "task": task["task"][:100],
                        "content": str(output)[:500],
                    })
        except Exception as e:
            print(f"[Alfred] Scheduled task execution failed: {e}")

    def pop_alerts(self) -> List[Dict]:
        """Get and clear pending alerts (for WebSocket broadcast)."""
        alerts = list(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts

    async def _execute_heartbeat(self):
        """Execute heartbeat tasks and push results to Cockpit via WebSocket."""
        now = datetime.now()
        if now.hour < 7 or now.hour > 23:
            return

        print(f"[Alfred] Heartbeat triggered at {now.strftime('%H:%M')}")

        # T1 auto-expiration
        from .memory.five_tier import get_memory
        expired = get_memory().t1_clear_expired()
        if expired:
            print(f"[Alfred] Heartbeat: cleared {expired} expired T1 items")

        client = GWSClient()

        # Check calendar
        try:
            output = client.get_agenda(days=7)
            if output and "No upcoming events" not in output:
                self._pending_alerts.append({"type": "heartbeat", "source": "calendar", "content": output[:500]})
                print(f"[Alfred] Calendar events found: {output[:200]}")
        except Exception as e:
            print(f"[Alfred] Calendar heartbeat failed: {e}")

        # Check email
        try:
            output = client.triage_emails()
            if output and "No new emails" not in output:
                self._pending_alerts.append({"type": "heartbeat", "source": "email", "content": output[:500]})
                print(f"[Alfred] Email triage: {output[:200]}")
        except Exception as e:
            print(f"[Alfred] Email heartbeat failed: {e}")

        # Cognitive pulse — brief self-reflection every heartbeat
        try:
            from .memory.five_tier import get_memory
            recent_t3 = get_memory().t3_search("recent activity", top_k=3)
            if recent_t3:
                print(f"[Alfred] Cognitive pulse: {len(recent_t3)} recent episodes")
        except Exception as e:
            print(f"[Alfred] Cognitive pulse failed: {e}")

        # Morning briefing at 7am
        if now.hour == 7 and now.minute < 5:
            briefing = await self._generate_morning_briefing()
            if briefing:
                self._pending_alerts.append({"type": "heartbeat", "source": "briefing", "content": briefing[:500]})
                print("[Alfred] Morning briefing generated")

    async def _generate_morning_briefing(self) -> str:
        """Generate daily morning briefing."""
        briefing_parts = []
        client = GWSClient()

        # Calendar
        try:
            output = client.get_agenda(days=1)
            if output and "error" not in output.lower():
                briefing_parts.append(f"## Calendar\n{output[:300]}")
        except Exception:
            pass

        # Email triage
        try:
            output = client.triage_emails(max_results=5)
            if output and "error" not in output.lower():
                briefing_parts.append(f"## Email\n{output[:300]}")
        except Exception:
            pass

        if briefing_parts:
            return "\n\n".join(briefing_parts)
        return ""

# ============ TOOL HEALTH ============

    def _check_tool_health(self) -> Dict[str, str]:
        """Pre-check which tools are available. Returns {tool_name: 'ok'|'fail:reason'}."""
        health = {}
        for name, info in self._tools.items():
            try:
                if name in ("gws", "calendar", "email"):
                    client = GWSClient()
                    hc = client.health_check()
                    is_ok = "FAILED" not in hc and "NOT GRANTED" not in hc
                    health[name] = "ok" if is_ok else f"fail:{hc}"
                elif name == "weather":
                    import requests as _rq
                    _rq.get("https://wttr.in", timeout=3)
                    health[name] = "ok"
                elif name == "web_search":
                    health[name] = "ok"
                elif name == "web_fetch":
                    health[name] = "ok"
                elif name == "screenshot":
                    health[name] = "ok"
                else:
                    health[name] = "ok"
            except Exception as e:
                health[name] = f"fail:{e}"
        return health

    # ============ TOOL CALL VALIDATION ============

    def _validate_tool_call(self, tool: str, params: Dict) -> Dict:
        """Validate a tool call before execution. Returns {'valid': bool, 'reason': str}."""
        if tool not in self._tools:
            return {"valid": False, "reason": f"Unknown tool '{tool}'. Available: {list(self._tools.keys())}"}
        info = self._tools[tool]
        required = [k for k in info.get("params", {}) if k in ("expression", "url", "code", "command", "path", "message", "text", "when", "key", "value", "location")]
        if tool not in ("calendar", "email", "gws"):
            required.extend([k for k in info.get("params", {}) if k == "query"])
        for r in required:
            if r not in params or not params.get(r):
                return {"valid": False, "reason": f"Tool '{tool}' requires param '{r}'"}
        if tool == "shell":
            blocked = ["rm -rf", "del /f /s /q", "mkfs"]
            cmd = params.get("command", "").lower()
            if any(b in cmd for b in blocked):
                return {"valid": False, "reason": "Command blocked: potentially dangerous"}
        if tool == "run_code":
            if not params.get("code"):
                return {"valid": False, "reason": "run_code requires 'code' param"}
        return {"valid": True, "reason": ""}

    # ============ REPLY CLASSIFICATION ============

    def _classify_reply(self, response: str, task: str) -> str:
        """Classify a model's reply to decide if the loop should terminate.
        Uses fast Groq model for accurate labeling.
        Returns: 'acknowledgement', 'clarification', 'refusal', 'genuine_answer'"""
        lower = response.lower().strip()

        # Fast path: obvious patterns that don't need an LLM call
        if not lower or len(lower) < 5:
            return "acknowledgement"

        # Try LLM classification via Groq
        try:
            import requests as _rq
            llm_resp = _rq.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": (
                        f"Classify this assistant reply into one label:\n"
                        f"- acknowledgement: says 'I'll do that', 'sure', 'got it', 'on it', etc. But NOT already completed.\n"
                        f"- clarification: asks the user a question back.\n"
                        f"- refusal: says can't do it, unable, not configured, not able to.\n"
                        f"- genuine_answer: gives actual information, results, or confirms completion.\n\n"
                        f"Task: {task[:200]}\nReply: {response[:300]}\n\nLabel:"
                    )}],
                    "max_tokens": 5,
                    "temperature": 0.0,
                },
                headers={"Authorization": f"Bearer {self._groq_key}"},
                timeout=5,
            )
            if llm_resp.status_code == 200:
                label = llm_resp.json()["choices"][0]["message"]["content"].strip().lower()
                for valid in ("acknowledgement", "clarification", "refusal", "genuine_answer"):
                    if valid in label:
                        return valid
        except Exception:
            pass

        # Fallback: lightweight heuristic
        if re.match(r'^(i\'?ll\s+(do|check|try|look|see)|let me\s+(do|check|try)|on it|got it)', lower):
            return "acknowledgement"
        if re.search(r'\b(cannot|can\'t\s+do|unable\s+to\s+(complete|fulfill|process)|don\'t have access to)\b', lower):
            return "refusal"
        if "?" in response:
            return "clarification"
        return "genuine_answer"

    # ============ CONTEXT WINDOW MANAGEMENT ============

    def _compact_context(self, ctx: TaskContext, max_chars: int = 6000) -> TaskContext:
        """Compact execution history to fit within context window.
        Summarizes old results, keeps recent ones intact."""
        total = sum(len(r.result or "") for r in ctx.execution_results)
        if total <= max_chars:
            return ctx

        # Keep last 2 results intact, summarize everything before
        if len(ctx.execution_results) > 2:
            preserved = ctx.execution_results[-2:]
            summarized = ctx.execution_results[:-2]
            for r in summarized:
                if r.result and len(r.result) > 100:
                    r.result = f"[{len(r.result)} chars omitted] {r.result[:100]}..."
            ctx.execution_results = summarized + preserved
        else:
            # All results are recent — truncate each
            for r in ctx.execution_results:
                if r.result and len(r.result) > 500:
                    r.result = r.result[:500] + f"... [{len(r.result)-500} more chars]"

        # Trim tool_calls_made to last 10
        if len(ctx.tool_calls_made) > 10:
            ctx.tool_calls_made = ctx.tool_calls_made[-10:]

        return ctx

    # ============ TOKEN ESTIMATE ============

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate (4 chars per token)."""
        return len(text) // 4

    # ============ EXECUTE WITH RETRY ============

    async def _execute_with_retry(self, tool: str, params: Dict, ctx: TaskContext, context: Dict = None) -> Dict:
        """Execute a tool with error classification, retry, and alternative approaches."""
        max_retries = 3
        last_error = ""
        backoff = 1

        for attempt in range(max_retries):
            result = await self._execute_tool(tool, params, context)
            if not result.get("error"):
                return result

            error = result["error"]
            last_error = error
            ctx.corrections_made += 1

            # Classify error using ErrorClassifier
            from .errors import get_error_classifier
            classifier = get_error_classifier()
            classified = classifier.classify(error, tool=tool)
            recovery_method = classifier.get_recovery_strategy(classified)
            strategy = recovery_method(classified)

            if strategy["action"] == "retry" and attempt < max_retries - 1:
                delay = strategy.get("delay", backoff)
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, 30)
                continue
            elif strategy["action"] in ("notify", "decline"):
                break
            elif strategy["action"] == "search_memory":
                # Semantic error — try once more
                if attempt < max_retries - 1:
                    continue
                break
            else:
                if attempt < max_retries - 1:
                    continue

        # Try smart recovery before giving up
        task_str = (context or {}).get("task", "") or tool
        try:
            new_plan = await self._handle_error_smart(task_str, ctx, context)
            if new_plan:
                return {"output": f"Smart recovery: trying alternative approach with {new_plan[0]['tool']}", "new_plan": new_plan}
        except Exception:
            pass
        return {"error": f"Failed after {max_retries} attempts: {last_error}"}

    # ============ REFLECTION CHECK ============

    async def _reflection_check(self, task: str, ctx: TaskContext) -> bool:
        """Ask the model if we're still on track to fulfill the original request.
        Returns True if on track, False if we've drifted."""
        if len(ctx.execution_results) < 3:
            return True  # Not enough data to judge yet

        steps_summary = "\n".join(
            f"  {i+1}. {r.tool}: {(r.result or '')[:100]}"
            for i, r in enumerate(ctx.execution_results[-5:])
        )

        prompt = f"""Task: {task}
Recent steps:
{steps_summary}

Is the task complete? Answer ONLY: 'yes' or 'no' with a one-sentence reason."""

        result = await self._call_llm(
            "You are a task auditor. Be strict — only say yes if the original request is fully satisfied.",
            prompt, model=self.PLANNER_MODEL, max_tokens=100, temperature=0.1
        )
        return "yes" in result.lower()[:10] if result else True

    # ============ MODEL FALLBACK CHAIN ============

    async def _call_llm_with_fallback(self, system_prompt: str, user_message: str, messages: list = None, max_tokens: int = 800, temperature: float = 0.3) -> str:
        """Call LLM via router with 3-provider fallback chain."""
        router_response = await self._router.call(
            system_prompt=system_prompt,
            user_message=user_message,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return router_response.text or ""

    # ============ DYNAMIC MAX ROUNDS ============

    def _calculate_max_rounds(self, plan_length: int, task: str) -> int:
        """Calculate dynamic max iterations based on plan length and task complexity."""
        base = max(plan_length * 2, 5)
        task_lower = task.lower()
        # Research-heavy tasks need more rounds
        if any(w in task_lower for w in ["research", "investigate", "analyze", "compare", "find all"]):
            base += 5
        # Simple tasks need fewer
        if any(w in task_lower for w in ["what", "when", "who", "is", "hi", "hello"]):
            base = min(base, 3)
        return min(base, 20)  # Cap at 20

    # ============ EXECUTE (HARDENED LOOP) ============

    async def execute(self, task: str, context: Dict = None) -> Dict[str, Any]:
        """Main entry point: Execute task through Alfred's hardened 4-phase loop.
        Addresses: error handling, validation, context management, reflection, fallbacks."""
        ctx = TaskContext(original_task=task)
        thinking = []

        session_id = None
        if context:
            session_id = context.get("session_id")

        conversation_history = context.get("conversation_history", []) if context else []

        self.memory.t1_set("current_task", task)
        self.memory.t1_set("task_start", datetime.now().isoformat())

        try:
            # ===== PHASE 0: PREPARATION =====
            thinking.append("Pre-flight checks...")

            # Tool health check (cached per session, invalidated after 5 min)
            now = time.time()
            if not hasattr(self, '_tool_health_cache') or not hasattr(self, '_tool_health_ts'):
                self._tool_health_cache = self._check_tool_health()
                self._tool_health_ts = now
            elif now - self._tool_health_ts > 300:
                self._tool_health_cache = self._check_tool_health()
                self._tool_health_ts = now
            tool_health = self._tool_health_cache
            broken_tools = [t for t, s in tool_health.items() if s != "ok"]
            if broken_tools:
                thinking.append(f"Unavailable tools: {broken_tools}")
                print(f"[Alfred] Tools unavailable: {broken_tools}", flush=True)

            # Goal inference
            planning_task = task
            if self.goal_inference_enabled:
                thinking.append("Expanding goal...")
                goal_inference_result = await self.goal_expander.expand(task)
                if goal_inference_result and goal_inference_result.expanded:
                    planning_task = goal_inference_result.expanded
                    thinking.append(f"Expanded: {planning_task[:100]}")

            # Intent classification
            thinking.append("Classifying intent...")
            intent_result = self.intent_classifier.classify(task)
            thinking.append(f"Intent: {intent_result['category']} ({intent_result['confidence']})")

            # Skip plan + T3 for conversational intents — route directly to chat
            is_conversational = intent_result["category"] in ("general", "greeting", "casual", "chat")
            if is_conversational:
                thinking.append("Conversational intent — skipping plan generation")
                chat_result = await self._execute_tool("chat", {"message": task, "conversation_history": conversation_history}, context)
                response_text = chat_result.get("output", "Done.")
                return {
                    "response": response_text,
                    "phase": "completed",
                    "steps": [],
                    "skill_used": False,
                    "skill_generated": False,
                    "thinking": thinking,
                }

            # Search T3 episodic memory for relevant past experiences
            t3_context = None
            t3_results = self.memory.t3_find_episodes(task, max_results=3)
            if t3_results:
                t3_context = "\n".join(
                    f"- {r.get('title', 'Untitled')}: {r.get('content', '')[:150]}"
                    for r in t3_results[:3]
                )
                thinking.append(f"Found {len(t3_results)} relevant past episodes")

            # === T2 SKILL MATCHING — before plan generation ===
            matched_skill = self.skill_manager.find_skill(planning_task)
            if matched_skill:
                thinking.append(f"T2 skill matched: {matched_skill.title}")
                plan = matched_skill.steps
                has_skill_match = True
            else:
                has_skill_match = False

            if not has_skill_match:
                # Generate plan: keyword fallback for simple queries, LLM for complex
                thinking.append("Generating execution plan...")
            complex_task = any(m in planning_task.lower() for m in [" and ", ", ", " then ", " also ", " plus "])
            if not complex_task:
                plan = self._fallback_plan(planning_task)
            else:
                plan = None
            if not plan or len(plan) == 0 or plan[0].get("tool") == "chat" or complex_task:
                if not plan:
                    thinking.append("No keyword match — trying LLM planner")
                else:
                    thinking.append("Complex task — trying LLM planner")
                try:
                    plan = await asyncio.wait_for(
                        self._llm_generate_plan(planning_task, context, intent_result, t3_context),
                        timeout=12.0
                    )
                except asyncio.TimeoutError:
                    print("[Alfred] Plan generation timed out", flush=True)
                    thinking.append("Plan generation timed out")
                except Exception as e:
                    print(f"[Alfred] Plan generation error: {e}", flush=True)
                    thinking.append(f"Plan generation error: {e}")

                if plan and intent_result:
                    chat_only = all(s.get("tool") == "chat" for s in plan)
                    if chat_only and not is_conversational:
                        thinking.append("LLM gave all-chat — using intent-based plan")
                        plan = self._intent_based_plan(task, intent_result)

                if not plan:
                    plan = self._fallback_plan(planning_task)
                    if plan:
                        thinking.append(f"  Last-resort fallback: {plan[0].get('tool', '?')}")
            if plan:
                for step in plan[:5]:
                    thinking.append(f"  Plan: {step.get('description', step['tool'])[:80]}")
                if len(plan) > 5:
                    thinking.append(f"  ... and {len(plan)-5} more steps")
            else:
                thinking.append("No plan generated — will improvise")

            # Dynamic max rounds
            max_iterations = self._calculate_max_rounds(len(plan) if plan else 0, task)
            thinking.append(f"Max rounds: {max_iterations}")

            # ===== PHASES 1-N: ITERATIVE EXECUTION =====
            ctx.current_phase = Phase.EXECUTION
            total_cost_estimate = 0
            tool_calls = 0
            consecutive_same_tool = 0
            last_tool_params = None

            for iteration in range(max_iterations):
                # Context window management — compact if needed
                ctx = self._compact_context(ctx)

                # Determine next action
                action = None

                # Try to follow the pre-generated plan first
                if plan and iteration < len(plan):
                    step = plan[iteration]
                    action = {
                        "type": "tool",
                        "tool": step["tool"],
                        "params": step.get("params", {}),
                        "description": step.get("description", ""),
                    }
                    thinking.append(f"Plan step {iteration+1}/{len(plan)}: {step.get('description', step['tool'])[:80]}")
                else:
                    # Plan exhausted — if we have results, wrap up
                    if ctx.execution_results and iteration >= len(plan):
                        # Don't re-ask the model — format what we have
                        break
                    # Ask the model what to do next (no plan or plan + need more)
                    action = await self._get_next_action(task, ctx, t3_context, intent_result)

                if action["type"] == "reply":
                    response_text = action.get("response", "")
                    reply_class = self._classify_reply(response_text, task)

                    if reply_class == "acknowledgement":
                        thinking.append(f"Ack: {response_text[:60]} — forcing tool call")
                        # Re-prompt with stronger force
                        action = await self._get_next_action(task, ctx, t3_context, intent_result)
                        if action["type"] == "reply":
                            reply_class = self._classify_reply(action.get("response", ""), task)
                            if reply_class == "acknowledgement":
                                action = await self._get_next_action(task, ctx, t3_context, intent_result)
                                if action["type"] == "reply":
                                    reply_class = self._classify_reply(action.get("response", ""), task)

                    if reply_class == "clarification":
                        thinking.append(f"Clarification needed: {response_text[:80]}")
                        ctx.final_response = response_text
                        ctx.current_phase = Phase.REFLECTION
                        break

                    if reply_class == "refusal":
                        thinking.append(f"Refusal: {response_text[:80]}")
                        ctx.final_response = response_text
                        ctx.current_phase = Phase.REFLECTION
                        break

                    # Genuine answer or fallback
                    if ctx.execution_results or tool_calls > 0:
                        # Has done work — accept the reply as completion
                        ctx.final_response = response_text
                        thinking.append(f"Model completed: {response_text[:80]}")
                        ctx.current_phase = Phase.REFLECTION
                        break
                    else:
                        # No work done — force tool call
                        thinking.append("No tools called yet — forcing tool call")
                        continue

                # === TOOL EXECUTION PATH ===
                tool_name = action.get("tool", "chat")
                tool_params = action.get("params", {})
                tool_description = action.get("description", "")

                # Validate tool call before execution
                validation = self._validate_tool_call(tool_name, tool_params)
                if not validation["valid"]:
                    thinking.append(f"Invalid: {validation['reason']}")
                    if iteration < (max_iterations - 1):
                        continue  # Skip and try again
                    else:
                        ctx.final_response = f"I couldn't complete this: {validation['reason']}"
                        break

                # Check if tool is healthy
                if tool_name in tool_health and tool_health[tool_name] != "ok":
                    thinking.append(f"Tool {tool_name} unavailable: {tool_health[tool_name]}")
                    continue

                # Track consecutive same tool+params for loop detection
                current_tool_params = (tool_name, str(sorted(tool_params.items())))
                if current_tool_params == last_tool_params:
                    consecutive_same_tool += 1
                else:
                    consecutive_same_tool = 0
                last_tool_params = current_tool_params

                # Same tool+params 4x in a row — force break
                if consecutive_same_tool >= 4:
                    thinking.append(f"Loop detected: {tool_name} called 4x with identical params")
                    ctx.final_response = "Completed after multiple attempts."
                    break

                thinking.append(f"Iteration {iteration+1}: {tool_name} {tool_description}")

                # Track cost (rough estimate)
                total_cost_estimate += 1

                # Execute with retry and error handling
                result = await self._execute_with_retry(tool_name, tool_params, ctx, {**(context or {}), "task": task})
                output = result.get("output", "")
                error = result.get("error", "")

                ctx.add_result(ToolResult(
                    tool=tool_name,
                    params=tool_params,
                    result=output or error,
                    error=error or None,
                ))
                tool_calls += 1
                ctx.tool_calls_made.append(tool_name)

                # Summarize large tool outputs to prevent context overflow
                if output and len(output) > 4000:
                    line_count = output.count('\n') + 1
                    summary = f"[Tool output: {len(output)} chars, {line_count} lines — summarized]"
                    # Try to extract numeric counts from the output
                    numbers = re.findall(r'\b(\d{2,})\b', output)
                    if numbers:
                        summary += f" Key numbers found: {', '.join(numbers[:5])}"
                    # Keep first and last 500 chars
                    summary += f"\n--- First 500 chars ---\n{output[:500]}\n--- Last 500 chars ---\n{output[-500:]}"
                    ctx.add_result(ToolResult(
                        tool=f"{tool_name}_summary",
                        params={"original_length": len(output), "lines": line_count},
                        result=summary,
                    ))
                    # Replace the large output with summary for context
                    output = summary

                if error:
                    thinking.append(f"  X Error: {error[:100]}")
                    # If this is a mutation tool, anchor the response to the error — LLM cannot claim success
                    if tool_name in ("write_file", "calendar", "email", "memory_save", "set_reminder"):
                        ctx.final_response = f"The {tool_name} tool failed: {error[:300]}"
                        break
                else:
                    thinking.append(f"  ✓ OK ({len(output)} chars)")

                # === VERIFICATION LOOP: check mutation tools actually worked ===
                if not error and tool_name in ("write_file", "calendar", "email", "memory_save", "set_reminder"):
                    thinking.append(f"  Verifying {tool_name} result...")
                    verify_ok = await self._verify_mutation(tool_name, tool_params, output)
                    if not verify_ok:
                        thinking.append(f"  Verification FAILED for {tool_name} — anchoring response to actual tool output")
                        # Force the response to the tool's actual output — the LLM cannot override this
                        forced_response = output if output else f"The {tool_name} tool reported an issue. Output: {output or 'empty'}"
                        ctx.final_response = forced_response
                        ctx.add_result(ToolResult(
                            tool=f"{tool_name}_verify",
                            params=tool_params,
                            result=f"VERIFICATION FAILED: Response anchored to actual tool output. LLM cannot claim success.",
                            error="Verification failed",
                        ))
                        break
                    else:
                        thinking.append("  Verification passed")

                # Mid-task reflection every 5 iterations
                if iteration > 0 and iteration % 5 == 0:
                    on_track = await self._reflection_check(task, ctx)
                    if on_track:
                        thinking.append("Self-check: on track ✓")
                    else:
                        thinking.append("Self-check: may be drifting — re-focusing")
                        ctx.add_result(ToolResult(
                            tool="_reflection",
                            params={},
                            result=f"RE-FOCUS: You may have drifted from the original task. Re-focus on: {task[:200]}",
                        ))

                # Cost cap: stop if too expensive
                if total_cost_estimate > max_iterations:
                    thinking.append("Cost limit reached — stopping")
                    break

            # ===== FINAL: REFLECTION =====
            ctx.current_phase = Phase.REFLECTION
            thinking.append(f"Task completed. Tool calls: {tool_calls}, Cost estimate: {total_cost_estimate}")

            # Generate skill for complex tasks
            successful_steps = [
                {"tool": r.tool, "params": r.params, "result": r.result}
                for r in ctx.execution_results
                if r.tool and not r.error
            ]
            has_fatal_error = any(r.error for r in ctx.execution_results if r.error)
            generated_skill = False
            if not has_fatal_error and tool_calls >= 5:
                thinking.append("Generating skill...")
                complexity = "simple" if tool_calls <= 2 else "moderate" if tool_calls <= 5 else "complex"
                generated = self.skill_manager.generate_skill(
                    task=task, steps=successful_steps, task_complexity=complexity,
                    had_error=ctx.corrections_made > 0,
                )
                if generated:
                    generated_skill = True
                    thinking.append(f"Skill: {generated.title}")

            # Format response — preserve model's reply if already set
            if not ctx.final_response:
                formatted = await self._format_response(task, ctx)
                ctx.final_response = formatted or "Task completed."

            # Save to T3 episodic memory (AFTER response is set)
            if tool_calls > 0:
                thinking.append("Saving to memory...")
                safe_title = re.sub(r'[^a-zA-Z0-9 ]', '', task[:50]).strip().replace(' ', '-')
                tools_used = ', '.join(set(ctx.tool_calls_made))
                episode_content = (
                    f"## User Request\n{task}\n\n"
                    f"## Alfred Response\n{ctx.final_response[:2000]}\n\n"
                    f"## Execution Details\n"
                    f"- Tools used: {tools_used}\n"
                    f"- Tool calls: {tool_calls}\n"
                    f"- Success: {not has_fatal_error}\n"
                    f"- Cost estimate: {total_cost_estimate}"
                )
                self.memory.t3_save_episode(
                    title=f"Task {safe_title}",
                    content=episode_content,
                    metadata={
                        "tool_calls": tool_calls,
                        "tools": tools_used,
                        "success": not has_fatal_error,
                        "cost": total_cost_estimate,
                    },
                )

            return {
                "response": ctx.final_response,
                "phase": ctx.current_phase.value,
                "thinking": thinking,
                "steps": [{"tool": s.tool, "params": s.params, "result": s.result} for s in ctx.plan],
                "skill_generated": generated_skill,
                "intent": intent_result,
                "session_id": session_id,
                "tool_calls": tool_calls,
                "cost_estimate": total_cost_estimate,
            }

        except Exception as e:
            thinking.append(f"Fatal error: {str(e)}")
            print(f"[Alfred] Execute error: {traceback.format_exc()}", flush=True)
            return {
                "response": f"I encountered an error: {str(e)}",
                "phase": "error",
                "thinking": thinking,
                "session_id": session_id,
            }

    async def execute_voice(self, audio_samples, context: Dict = None) -> Dict[str, Any]:
        """Execute a voice command: transcribe audio → route through same Hermes loop.
        Voice messages go through the EXACT same agent loop as text."""
        # Transcribe
        thinking = ["Transcribing voice input..."]
        text = self._transcribe_voice(audio_samples)
        if not text:
            return {
                "response": "I couldn't hear you. Please try again.",
                "phase": "error",
                "thinking": thinking + ["No speech detected"],
                "session_id": context.get("session_id") if context else None,
            }

        thinking.append(f"Transcribed: '{text}'")

        # Route through same execute loop
        result = await self.execute(text, context)
        result["thinking"] = thinking + result.get("thinking", [])
        result["voice"] = True
        return result

    def _transcribe_voice(self, audio_samples) -> str:
        """Transcribe audio samples to text using faster-whisper."""
        try:
            from .voice import SpeechToText, VoiceConfig
            stt = SpeechToText(VoiceConfig())
            if stt.load():
                return stt.transcribe(audio_samples)
            return ""
        except ImportError:
            return ""
        except Exception as e:
            print(f"[Alfred] Voice transcription failed: {e}")
            return ""

    async def speak_response(self, text: str) -> bool:
        """Convert text to speech using edge-tts."""
        try:
            from .voice import TextToSpeech, VoiceConfig
            tts = TextToSpeech(VoiceConfig())
            audio = await tts.speak(text)
            return len(audio) > 0
        except ImportError:
            return False
        except Exception as e:
            print(f"[Alfred] TTS failed: {e}")
            return False

    def _get_gemini_client(self):
        """Lazy init Gemini client."""
        if self._gemini_client is None and self._google_key:
            from google import genai
            self._gemini_client = genai.Client(api_key=self._google_key)
        return self._gemini_client

    def _get_openrouter_client(self):
        """Lazy init OpenRouter client (OpenAI-compatible)."""
        if self._openrouter_client is None and self._openrouter_key:
            from openai import OpenAI
            self._openrouter_client = OpenAI(
                api_key=self._openrouter_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=15.0,
                max_retries=0,
            )
        return self._openrouter_client

    async def _call_llm(self, system_prompt: str, user_message: str, model: str = None, messages: list = None, max_tokens: int = 800, temperature: float = 0.3, reasoning: bool = False) -> str:
        model = model or self.PLANNER_MODEL
        try:
            if model.startswith("gemini"):
                client = self._get_gemini_client()
                if not client:
                    return ""
                from google.genai import types
                response = client.models.generate_content(
                    model=model,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=max_tokens,
                        temperature=temperature,
                    ),
                )
                return response.text.strip()
            elif "/" in model:
                # OpenRouter model (e.g. "deepseek/deepseek-v4-flash:free")
                client = self._get_openrouter_client()
                if not client:
                    return ""
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                    ] + (messages if messages else [{"role": "user", "content": user_message}]),
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                content = response.choices[0].message.content
                if content:
                    return content.strip()
                return ""
            client = self._get_groq_client()
            if not client:
                return ""
            kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                ] + (messages if messages else [{"role": "user", "content": user_message}]),
                temperature=temperature,
                stop=None,
            )
            if reasoning:
                kwargs["max_completion_tokens"] = max_tokens
                kwargs["reasoning_effort"] = "medium"
            else:
                kwargs["max_tokens"] = max_tokens
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if content:
                return content.strip()
            return ""
        except Exception as e:
            print(f"[Alfred] LLM call failed ({model}): {e}", flush=True)
            return ""

    async def _llm_generate_plan(self, task: str, context: Dict = None, intent: Dict = None, t3_context: str = None) -> List[Dict]:
        """Generate execution plan using 70B planner model."""
        intent_hint = f"Detected intent: {intent['category']} (confidence: {intent['confidence']})" if intent else ""
        t3_hint = f"\n## PAST EXPERIENCES\n{t3_context}" if t3_context else ""
        tool_descriptions = self._get_tool_descriptions()

        system_prompt = f"""You are Alfred, an autonomous AI executor. Generate a step-by-step plan using available tools.

RULES:
1. NEVER use 'chat' unless it's pure greeting/small talk. Use real tools.
2. Return ONLY a JSON array. No explanation. No markdown.
3. Keys per step: "tool", "description", "params".

TOOL MAP:
- reminder/schedule → set_reminder | recall/list → list_reminders
- save fact → remember | search memory → memory_search
- web search → web_search | system → shell | file → read_file/write_file
- calendar → calendar (action: agenda/list/create/delete)
- email → email (action: triage/send/read/list)
- math → calculator | time/date → time

{tool_descriptions}

{intent_hint}{t3_hint}

EXAMPLES:
"Remind me to call mom at 10am" → [{{"tool":"set_reminder","description":"Call mom reminder","params":{{"text":"Call mom","when":"tomorrow 10:00"}}}}]
"Search AI news" → [{{"tool":"web_search","description":"AI news","params":{{"query":"AI news 2026","num_results":5}}}}]
"Send email to x@y.com" → [{{"tool":"email","description":"Send email","params":{{"action":"send","to":"x@y.com","subject":"...","body":"..."}}}}]
"Show my calendar" → [{{"tool":"calendar","description":"List events","params":{{"action":"agenda"}}}}]
"Add study block for Physics tomorrow at 4pm" → [{{"tool":"calendar","description":"Create Physics study block","params":{{"action":"create","summary":"Physics study block","start_time":"2026-07-22 16:00:00","end_time":"2026-07-22 17:00:00"}}}}]
"What's my favorite color?" → [{{"tool":"memory_search","description":"Look up color","params":{{"query":"favorite color","tier":"t4"}}}}]"""

        content = await self._call_llm(system_prompt, task, model=self.PLANNER_MODEL, max_tokens=2000, temperature=0.5)
        if not content:
            return self._fallback_plan(task)

        # Extract JSON from response
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            try:
                plan = json.loads(json_match.group())
                if isinstance(plan, list) and len(plan) > 0:
                    return plan
            except Exception:
                pass

        return self._fallback_plan(task)

    async def _get_next_action(self, task: str, ctx: TaskContext, t3_context: str = None, intent: Dict = None) -> Dict:
        """Ask the planner model what to do next."""
        history = "  (no steps yet)"
        if ctx.execution_results:
            parts = []
            for i, r in enumerate(ctx.execution_results[:5]):
                result_preview = (r.result or "")[:300]
                error_suffix = f" ERROR: {r.error[:100]}" if r.error else ""
                parts.append(f"  {i+1}. {r.tool}: {result_preview}{error_suffix}")
            if len(ctx.execution_results) > 5:
                parts.append(f"  ... and {len(ctx.execution_results)-5} more steps")
            history = "\n".join(parts)

        # Include T4 user profile context (compact)
        t4_hint = ""
        try:
            t4_ctx = self.memory.get_context_for_llm()
            if t4_ctx and "User Profile" in t4_ctx:
                t4_lines = [line for line in t4_ctx.split("\n") if line.strip() and ":" in line]
                if t4_lines:
                    t4_hint = "\n".join(t4_lines[:10])
        except Exception:
            pass

        tool_descriptions = self._get_tool_descriptions()

        system_prompt = f"""You are Alfred, an autonomous AI assistant. **You MUST use tools — never just talk about doing something.**

## CURRENT CONTEXT
Task: {task}
Execution history:
{history}

Current time: {datetime.now().strftime('%Y-%m-%d %H:%M %A')}
{f"User facts:\n{t4_hint}" if t4_hint else ""}

## RULES
1. NEVER say "I'll do that" — just DO IT. Call the tool NOW.
2. If no tool called yet, call one immediately. Do not reply without tool results.
3. If a tool error'd, try a different approach.
4. Only "reply" when task is complete or it's pure chat.

{tool_descriptions}

## FORMAT
Tool call: {{"type": "tool", "tool": "name", "params": {{"key": "value"}}}}
Reply: {{"type": "reply", "response": "your answer"}}"""

        content = await self._call_llm(system_prompt, task, model=self.PLANNER_MODEL, max_tokens=2000, temperature=0.5)
        if not content:
            return {"type": "reply", "response": "I couldn't figure out what to do next."}

        try:
            # Try to parse JSON from the response (may contain markdown fences or be truncated)
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip(), flags=re.DOTALL)
            # Repair truncated JSON: count braces and add missing closing ones
            open_braces = cleaned.count('{')
            close_braces = cleaned.count('}')
            if open_braces > close_braces:
                cleaned += '}' * (open_braces - close_braces)
            action = json.loads(cleaned)
            # Standard reply
            if action.get("type") == "reply":
                return {"type": "reply", "response": action.get("response", "Done.")}
            # Standard tool call: {"type": "tool", "tool": "name", "params": {}}
            if action.get("type") == "tool":
                return {"type": "tool", "tool": action.get("tool", "chat"), "params": action.get("params", {}), "description": action.get("description", "")}
            # Lenient: {"type": "memory_search", "tool": "memory_search", "params": {}}
            # where type IS a tool name (not "reply" or "tool")
            tool_name = action.get("type", action.get("tool", ""))
            if tool_name and tool_name in self._tools:
                return {"type": "tool", "tool": tool_name, "params": action.get("params", {}), "description": action.get("description", "")}
            # Lenient: {"tool": "memory_search", "params": {}} (no type field)
            if action.get("tool") and isinstance(action.get("params"), dict):
                return {"type": "tool", "tool": action["tool"], "params": action["params"], "description": action.get("description", "")}
        except json.JSONDecodeError:
            pass

        # Fallback: parse Owl Alpha's native <longcat_tool_call> format (may be unclosed)
        xml_match = re.search(r'<longcat_tool_call>(.+)', content, re.DOTALL)
        if xml_match:
            inner = xml_match.group(1).strip()
            # Strip closing tag if present
            if inner.endswith('</longcat_tool_call>'):
                inner = inner[:-len('</longcat_tool_call>')].strip()
            # inner could be JSON or raw tool name
            try:
                inner_action = json.loads(inner)
                if inner_action.get("type") == "tool":
                    return {"type": "tool", "tool": inner_action.get("tool", "chat"), "params": inner_action.get("params", {})}
                if inner_action.get("type") == "reply":
                    return {"type": "reply", "response": inner_action.get("response", "Done.")}
            except json.JSONDecodeError:
                pass
            # Raw tool name with <longcat_arg_key>...</longcat_arg_key> pairs
            args = {}
            for arg_match in re.finditer(r'<longcat_arg_key>(.+?)</longcat_arg_key>\s*(?:<longcat_arg_value>(.+?)</longcat_arg_value>)?', inner, re.DOTALL):
                key = arg_match.group(1).strip()
                val = (arg_match.group(2) or "").strip()
                args[key] = val
            if args:
                tool_name_match = re.match(r'(\w+)', inner)
                tool_name = tool_name_match.group(1) if tool_name_match else "chat"
                return {"type": "tool", "tool": tool_name, "params": args}
            # Just a tool name
            if re.match(r'^\w+$', inner):
                return {"type": "tool", "tool": inner, "params": {}}

        # Fallback: treat response as a final reply
        return {"type": "reply", "response": content}

    def _fallback_plan(self, task: str) -> List[Dict]:
        """Fallback rule-based plan generation if LLM fails."""
        task_lower = task.lower()

        # Time / date
        if any(w in task_lower for w in ["what time", "current time", "what's the time", "time is it", "what day", "today's date", "date", "what is happening"]):
            return [{"description": "Get current time", "tool": "time", "params": {}}]

        # Weather
        if any(w in task_lower for w in ["weather", "temperature", "forecast", "rain", "sunny", "cloudy", "humid"]):
            city = ""
            city_match = re.search(r'(?:in|at|for)\s+([A-Za-z\s]+)', task)
            if city_match:
                city = city_match.group(1).strip()
            return [{"description": f"Get weather for {city or 'auto'}", "tool": "weather", "params": {"location": city}}]

        # Casual conversation
        casual_words = ["hi", "hello", "hey", "sup", "wassup", "ok", "okay", "cool", "nice", "thanks", "thank you", "bye"]
        if task.strip() in casual_words or all(w in casual_words for w in task_lower.split()):
            return [{"description": "Casual chat", "tool": "chat", "params": {"message": task}}]

        # Math
        math_match = re.search(r'\b\d+\s*[+\-*/]\s*\d+\b', task)
        if math_match:
            expr = math_match.group().replace(" ", "")
            return [{"description": f"Calculate {task}", "tool": "calculator", "params": {"expression": expr}}]

        # Calendar
        if any(w in task_lower for w in ["calendar", "schedule", "meeting", "appointment", "agenda"]):
            if any(w in task_lower for w in ["create", "add", "new", "schedule", "book", "set up"]):
                # Try to extract time from task for fallback
                import re as _re
                time_match = _re.search(r'(\d{1,2})\s*(am|pm|:?\d{0,2})', task_lower)
                start_time = ""
                if time_match:
                    hour = int(time_match.group(1))
                    suffix = time_match.group(2)
                    if "pm" in suffix and hour < 12:
                        hour += 12
                    elif "am" in suffix and hour == 12:
                        hour = 0
                    if not (0 <= hour <= 23):
                        hour = 16  # fallback to 4pm if parsing produced garbage
                    from datetime import datetime, timedelta
                    now = datetime.now()
                    if "tomorrow" in task_lower:
                        target = now + timedelta(days=1)
                    elif "monday" in task_lower:
                        days_ahead = (7 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "tuesday" in task_lower:
                        days_ahead = (1 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "wednesday" in task_lower:
                        days_ahead = (2 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "thursday" in task_lower:
                        days_ahead = (3 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "friday" in task_lower:
                        days_ahead = (4 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "saturday" in task_lower:
                        days_ahead = (5 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    elif "sunday" in task_lower:
                        days_ahead = (6 - now.weekday()) % 7 or 7
                        target = now + timedelta(days=days_ahead)
                    else:
                        target = now
                    try:
                        start_dt = target.replace(hour=hour, minute=0, second=0, microsecond=0)
                    except ValueError:
                        start_dt = target.replace(hour=16, minute=0, second=0, microsecond=0)
                    end_dt = start_dt + timedelta(hours=1)
                    start_time = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                return [{"description": "Create calendar event", "tool": "calendar", "params": {"action": "create", "summary": task, "start_time": start_time, "end_time": end_time}}]
            if any(w in task_lower for w in ["move", "change", "update", "reschedule", "edit"]):
                return [{"description": "Update calendar event", "tool": "calendar", "params": {"action": "create", "summary": task}}]
            return [{"description": "Check calendar", "tool": "calendar", "params": {"action": "agenda"}}]

        # Email
        if any(w in task_lower for w in ["email", "gmail", "inbox", "mail"]):
            return [{"description": "Check email", "tool": "email", "params": {"action": "triage"}}]

        # Reminders (order matters: check specific patterns first)
        if any(w in task_lower for w in ["list reminders", "show reminders", "my reminders", "pending reminders", "list all reminders"]):
            return [{"description": "List reminders", "tool": "list_reminders", "params": {}}]
        if "delete reminder" in task_lower:
            return [{"description": "Delete reminder", "tool": "delete_reminder", "params": {"id": ""}}]
        if any(w in task_lower for w in ["remind me", "set a reminder", "reminder", "remind"]):
            # Parse the reminder text and time from natural language
            reminder_text = task
            reminder_when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Strip "remind me to" or "remind me" prefix
            for prefix in ["remind me to ", "remind me that ", "remind me "]:
                if task_lower.startswith(prefix):
                    reminder_text = task[len(prefix):]
                    break
            # Check for "at <time>" patterns
            at_match = re.search(r'(?:at|by|before)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', task_lower)
            if at_match:
                hour = int(at_match.group(1))
                minute = int(at_match.group(2)) if at_match.group(2) else 0
                ampm = at_match.group(3)
                if ampm and ampm == "pm" and hour < 12:
                    hour += 12
                if ampm and ampm == "am" and hour == 12:
                    hour = 0
                now = datetime.now()
                reminder_when = now.replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%d %H:%M:%S")
                # Remove the time portion from the text
                reminder_text = re.sub(r'\s+(?:at|by|before)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?', '', reminder_text, count=1)
            return [{"description": "Set reminder", "tool": "set_reminder", "params": {"text": reminder_text.strip(), "when": reminder_when}}]

        # Memory — save (imperative: "remember that...", "save to memory...")
        if any(w in task_lower for w in ["save to memory", "store this", "save to my memory"]):
            content = task
            for prefix in ["save to memory: ", "save to my memory: "]:
                if task_lower.startswith(prefix):
                    content = task[len(prefix):]
                    break
            return [{"description": "Save to memory", "tool": "memory_save", "params": {"tier": "t3", "content": content, "title": ""}}]
        # "remember that...", "remember this...", "remember my...", "remember i..."
        if task_lower.startswith("remember that ") or task_lower.startswith("remember this "):
            content = re.sub(r'^remember (that|this) ', '', task, flags=re.I)
            return [{"description": "Save to memory", "tool": "memory_save", "params": {"tier": "t4", "content": content, "title": ""}}]
        if task_lower.startswith("remember i ") or task_lower.startswith("remember my "):
            content = re.sub(r'^remember (i|my) ', '', task, flags=re.I)
            return [{"description": "Save to memory", "tool": "memory_save", "params": {"tier": "t4", "content": content, "title": ""}}]
        # Memory — search (interrogative: "do you remember...")
        if any(w in task_lower for w in ["do you remember", "search your memory", "search memory", "what do you know about", "recall", "memory search", "search your memories", "search memories"]):
            return [{"description": "Search memory", "tool": "memory_search", "params": {"query": task, "tier": "all"}}]

        # Web search
        if any(w in task_lower for w in ["search", "find", "look up", "google"]):
            return [{"description": f"Search: {task}", "tool": "web_search", "params": {"query": task}}]

        # Default: chat
        return [{"description": "Handle with chat", "tool": "chat", "params": {"message": task}}]

    def _intent_based_plan(self, task: str, intent: Dict) -> List[Dict]:
        """Generate a plan based on intent category when LLM plan defaults to chat."""
        category = intent.get("category", "general")
        task_lower = task.lower()

        if category == "time":
            return [{"description": "Get current time", "tool": "time", "params": {}}]
        if category == "weather":
            return [{"description": "Get weather", "tool": "weather", "params": {}}]
        if category == "reminder":
            return [{"description": "Set reminder", "tool": "set_reminder", "params": {"text": task, "when": "now"}}]
        if category == "memory":
            if any(w in task_lower for w in ["do you remember", "search", "recall", "what is", "what's", "what are", "find"]):
                return [{"description": "Search memory", "tool": "memory_search", "params": {"query": task, "tier": "all"}}]
            return [{"description": "Save to memory", "tool": "remember", "params": {"key": "note", "value": task}}]
        # Preference queries often get wrong intent — catch them here
        if any(w in task_lower for w in ["my preference", "preference for", "what's my", "what is my", "do i like", "my favorite"]):
            return [{"description": "Search preferences", "tool": "memory_search", "params": {"query": task, "tier": "t4"}}]
        if category == "calendar":
            if any(w in task_lower for w in ["create", "add", "new", "schedule", "book"]):
                import re as _re
                time_match = _re.search(r'(\d{1,2})\s*(am|pm|:?\d{0,2})', task_lower)
                start_time = ""
                end_time = ""
                if time_match:
                    hour = int(time_match.group(1))
                    suffix = time_match.group(2)
                    if "pm" in suffix and hour < 12:
                        hour += 12
                    elif "am" in suffix and hour == 12:
                        hour = 0
                    if not (0 <= hour <= 23):
                        hour = 16
                    from datetime import datetime, timedelta
                    now = datetime.now()
                    if "tomorrow" in task_lower:
                        target = now + timedelta(days=1)
                    else:
                        target = now
                    try:
                        start_dt = target.replace(hour=hour, minute=0, second=0, microsecond=0)
                    except ValueError:
                        start_dt = target.replace(hour=16, minute=0, second=0, microsecond=0)
                    end_dt = start_dt + timedelta(hours=1)
                    start_time = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                return [{"description": "Create calendar event", "tool": "calendar", "params": {"action": "create", "summary": task, "start_time": start_time, "end_time": end_time}}]
            if any(w in task_lower for w in ["move", "change", "update", "reschedule", "edit"]):
                return [{"description": "Update calendar event", "tool": "calendar", "params": {"action": "create", "summary": task}}]
            return [{"description": "Check calendar", "tool": "calendar", "params": {"action": "agenda"}}]
        if category == "email":
            return [{"description": "Check email", "tool": "email", "params": {"action": "triage"}}]
        if category == "shell":
            if any(w in task_lower for w in ["python", "process", "background", "memory", "hog", "running", "kill", "stop"]):
                current_pid = os.getpid()
                return [
                    {"description": "List Python processes", "tool": "run_code", "params": {"code": f"Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -ne {current_pid} }} | Select-Object Id, ProcessName, StartTime, WorkingSet64 | Format-Table -AutoSize", "language": "shell"}},
                    {"description": "Kill old or high-memory Python processes", "tool": "run_code", "params": {"code": f"Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -ne {current_pid} -and ($_.StartTime -lt (Get-Date).AddDays(-1) -or $_.WorkingSet64 -gt 500MB) }} | Stop-Process -Force", "language": "shell"}},
                    {"description": "Verify cleanup", "tool": "run_code", "params": {"code": f"Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -ne {current_pid} }} | Select-Object Id, ProcessName, StartTime, WorkingSet64 | Format-Table -AutoSize", "language": "shell"}},
                ]
            return [{"description": "Run system command", "tool": "run_code", "params": {"code": task, "language": "shell"}}]
        if category == "file":
            return [
                {"description": "Read file", "tool": "read_file", "params": {"path": task}},
                {"description": "Write or edit file if needed", "tool": "write_file", "params": {"path": task, "content": ""}},
            ]
        if category == "web_search":
            return [{"description": "Search web", "tool": "web_search", "params": {"query": task, "num_results": 5}}]
        if category == "calculator":
            return [{"description": "Calculate", "tool": "calculator", "params": {"expression": task}}]

        # Unknown — try fallback
        return self._fallback_plan(task)

    async def _execute_tool(self, tool: str, params: Dict, context: Dict = None) -> Dict:
        """Execute a tool by name."""
        if tool not in self._tools:
            return {"error": f"Unknown tool: {tool}"}

        try:
            if tool == "chat":
                msg = params.get("message", "")
                if msg:
                    try:
                        # Inject T3 episodic context into chat tool
                        t3_ctx = None
                        try:
                            t3_results = self.memory.t3_find_episodes(msg, max_results=3)
                            if t3_results:
                                t3_parts = []
                                for r in t3_results:
                                    try:
                                        content = Path(r["path"]).read_text(encoding="utf-8")[:500]
                                        t3_parts.append(f"### {r['title']}\n{content}")
                                    except Exception:
                                        pass
                                if t3_parts:
                                    t3_ctx = "\n\n".join(t3_parts)
                        except Exception:
                            pass

                        history_msgs = params.get("conversation_history", [])
                        messages = []
                        for h in history_msgs:
                            messages.append({"role": h["role"], "content": h["content"]})

                        system_prompt = f"You are Alfred, Master Sam's autonomous AI assistant.\n\n{self._get_bootstrap_prompt(t3_context=t3_ctx)}"
                        router_response = await self._router.call(
                            system_prompt=system_prompt,
                            user_message=msg,
                            messages=messages if messages else None,
                            max_tokens=300,
                            temperature=0.5,
                        )
                        if router_response.text:
                            return {"output": router_response.text}
                        elif router_response.error:
                            return {"output": router_response.error}
                    except Exception as e:
                        print(f"[Alfred] Chat LLM failed: {e}", flush=True)
                        return {"output": f"[Chat error: {e}]"}
                return {"output": msg if msg else "Task completed."}

            elif tool == "calculator":
                expr = params.get("expression", "0")
                # Safe evaluation using only built-in math operators
                import ast
                import operator as _operator
                safe_ops = {
                    ast.Add: _operator.add, ast.Sub: _operator.sub,
                    ast.Mult: _operator.mul, ast.Div: _operator.truediv,
                    ast.Pow: _operator.pow, ast.USub: _operator.neg,
                    ast.FloorDiv: _operator.floordiv, ast.Mod: _operator.mod,
                }
                def _safe_eval(node):
                    if isinstance(node, ast.Expression):
                        return _safe_eval(node.body)
                    if isinstance(node, ast.Constant):
                        if isinstance(node.value, (int, float)):
                            return node.value
                        raise ValueError("Non-numeric constant")
                    if isinstance(node, ast.UnaryOp) and type(node.op) in safe_ops:
                        return safe_ops[type(node.op)](0, _safe_eval(node.operand))
                    if isinstance(node, ast.BinOp) and type(node.op) in safe_ops:
                        return safe_ops[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
                    raise ValueError(f"Unsupported operation: {type(node).__name__}")
                try:
                    tree = ast.parse(expr, mode='eval')
                    result = _safe_eval(tree)
                    if not isinstance(result, (int, float)):
                        result = "Invalid expression"
                except Exception:
                    return {"error": f"Invalid expression: {expr}"}
                return {"output": str(result)}

            elif tool == "calendar":
                print(f"[DEBUG-CAL] params={params}", flush=True)
                client = GWSClient()
                action = params.get("action", "agenda")
                if action == "agenda":
                    output = client.get_agenda(days=7)
                elif action == "list":
                    output = client.get_agenda(days=14)
                elif action == "create":
                    import re as _re
                    from datetime import datetime, timedelta
                    summary = params.get("summary", params.get("title", params.get("name", params.get("query", ""))))
                    start_time = params.get("start_time", params.get("start", params.get("date", "")))
                    end_time = params.get("end_time", params.get("end", ""))
                    description = params.get("description", "")
                    if not summary:
                        return {"error": "create action requires a summary/title for the event"}

                    # ALWAYS extract time from summary (user's intent) — don't trust LLM's time conversion
                    summary_lower = summary.lower()
                    time_match = _re.search(r'(\d{1,2})\s*:\s*(\d{2})\s*(am|pm)?', summary_lower)
                    if not time_match:
                        time_match = _re.search(r'(\d{1,2})\s*(am|pm)', summary_lower)
                    if time_match:
                        hour = int(time_match.group(1))
                        minute = int(time_match.group(2)) if time_match.lastindex >= 2 and time_match.group(2) and time_match.group(2).isdigit() else 0
                        suffix = time_match.group(3) if time_match.lastindex >= 3 else None
                        if not suffix and time_match.lastindex >= 2:
                            # Check if group(2) is am/pm (from the 2-group match)
                            g2 = time_match.group(2)
                            if g2 in ("am", "pm"):
                                suffix = g2
                                minute = 0
                        if suffix == "pm" and hour < 12:
                            hour += 12
                        elif suffix == "am" and hour == 12:
                            hour = 0
                        # Determine target date from summary
                        now = datetime.now()
                        if "tomorrow" in summary_lower:
                            target = now + timedelta(days=1)
                        elif "monday" in summary_lower:
                            target = now + timedelta(days=((7 - now.weekday()) % 7 or 7))
                        elif "tuesday" in summary_lower:
                            target = now + timedelta(days=((1 - now.weekday()) % 7 or 7))
                        elif "wednesday" in summary_lower:
                            target = now + timedelta(days=((2 - now.weekday()) % 7 or 7))
                        elif "thursday" in summary_lower:
                            target = now + timedelta(days=((3 - now.weekday()) % 7 or 7))
                        elif "friday" in summary_lower:
                            target = now + timedelta(days=((4 - now.weekday()) % 7 or 7))
                        elif "saturday" in summary_lower:
                            target = now + timedelta(days=((5 - now.weekday()) % 7 or 7))
                        elif "sunday" in summary_lower:
                            target = now + timedelta(days=((6 - now.weekday()) % 7 or 7))
                        else:
                            target = now
                        start_dt = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
                    elif not start_time:
                        return {"error": "create action requires start_time (ISO format like '2026-05-22 15:00:00')"}

                    if not end_time:
                        try:
                            dt = datetime.fromisoformat(start_time)
                            end_time = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
                        except Exception:
                            return {"error": "create action requires end_time or a valid start_time to auto-calculate"}
                    output = client.create_event(summary=summary, start_time=start_time, end_time=end_time, description=description)
                else:
                    output = client.get_agenda()

                # Ambiguity detection: if output has multiple events at same time and task involves moving/modifying
                if output and "Upcoming events" in output:
                    lines = output.split('\n')
                    event_lines = [line for line in lines if line.strip().startswith('-')]
                    if len(event_lines) >= 2:
                        # Check for duplicate times
                        times = re.findall(r'(\d{1,2}:\d{2}\s*[AP]M)', output, re.I)
                        if len(times) != len(set(times)):
                            # Duplicate times found — check if task involves modification
                            task_lower = context.get("task", "").lower() if context else ""
                            if any(w in task_lower for w in ["move", "reschedule", "update", "change", "shift", "delete", "cancel"]):
                                event_names = [re.search(r'-\s*(.+?)\s*\(', line).group(1) for line in event_lines if re.search(r'-\s*(.+?)\s*\(', line)]
                                return {"output": f"AMBIGUITY: Found {len(event_lines)} events at similar times: {', '.join(event_names)}. Which one should I {task_lower.split()[0]}?"}
                if "error" not in output.lower() or "Event created" in output:
                    return {"output": output[:500]}
                return {"error": output[:200]}

            elif tool == "email":
                client = GWSClient()
                action = params.get("action", "triage")
                if action in ("triage", "list"):
                    output = client.triage_emails()
                elif action == "read":
                    email_id = params.get("query", params.get("email_id", ""))
                    if not email_id:
                        return {"error": "read action requires email_id or query with email ID"}
                    output = client.read_email(email_id)
                elif action == "send":
                    to = params.get("to", "")
                    subject = params.get("subject", "")
                    body = params.get("body", params.get("query", params.get("message", "")))
                    if not to:
                        return {"error": "send action requires a 'to' email address"}
                    if not subject:
                        subject = "No Subject"
                    if not body:
                        body = ""
                    output = client.send_email(to=to, subject=subject, body=body)
                else:
                    output = client.triage_emails()
                if "error" not in output.lower() or "Email sent" in output:
                    return {"output": output[:500]}
                return {"error": output[:200]}

            elif tool == "gws":
                client = GWSClient()
                command = params.get("command", "")
                if not command:
                    return {"error": "No command specified"}
                output = client.run_generic(command)
                if "error" not in output.lower() or "not yet wrapped" in output.lower():
                    return {"output": output[:500]}
                return {"error": output[:200]}

            elif tool == "web_search":
                query = params.get("query", "")
                num = min(int(params.get("num_results", 5)), 10)
                return await self._web_search(query, num)

            elif tool == "web_fetch":
                url = params.get("url", "")
                return await self._web_fetch(url)

            elif tool == "shell":
                command = params.get("command", "")
                cwd = params.get("cwd")
                # Use PowerShell for all shell commands on Windows
                result = subprocess.run(["powershell", "-NoProfile", "-Command", command], cwd=cwd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0 or result.returncode == 1:
                    output = (result.stdout or "")[:2000]
                    if result.stderr:
                        output += f"\nSTDERR: {result.stderr[:500]}"
                    return {"output": output.strip()}
                return {"error": result.stderr[:500]}

            elif tool == "read_file":
                path = params.get("path", "")
                if not self._is_safe_path(path):
                    return {"error": "Path not in safe directories"}
                try:
                    offset = params.get("offset", 1) - 1
                    limit = params.get("limit", 2000)
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    content = "".join(lines[offset:offset + limit])
                    return {"output": content[:3000]}
                except Exception as e:
                    return {"error": str(e)}

            elif tool == "write_file":
                path = params.get("path", "")
                content = params.get("content", "")
                if not self._is_safe_path(path):
                    return {"error": "Path not in safe directories"}
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    return {"output": f"Written {len(content)} chars to {path}"}
                except Exception as e:
                    return {"error": str(e)}

            elif tool == "list_directory":
                path = params.get("path", ".")
                if not self._is_safe_path(path):
                    return {"error": "Path not in safe directories"}
                try:
                    entries = list(Path(path).iterdir())
                    lines = [f"{'DIR' if e.is_dir() else 'FILE'} {e.name}" for e in entries[:50]]
                    return {"output": "\n".join(lines)}
                except Exception as e:
                    return {"error": str(e)}

            elif tool == "glob":
                import glob as glob_lib
                pattern = params.get("pattern", "**/*")
                matches = glob_lib.glob(pattern, recursive=True)
                return {"output": "\n".join(matches[:50])}

            elif tool == "screenshot":
                try:
                    import pyautogui
                    ss_dir = Path(os.environ.get("TEMP", ".")) / "alfred_screenshots"
                    ss_dir.mkdir(parents=True, exist_ok=True)
                    path = ss_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    pyautogui.screenshot(str(path))
                    return {"output": f"Screenshot saved to {path}"}
                except Exception as e:
                    return {"error": str(e)}

            elif tool == "open_app":
                app_name = params.get("app_name", "").lower()
                app_map = {
                    "calculator": "calc",
                    "notepad": "notepad",
                    "chrome": "chrome",
                    "browser": "chrome",
                    "explorer": "explorer",
                    "settings": "ms-settings:",
                    "terminal": "wt",
                    "powershell": "powershell",
                    "cmd": "cmd",
                    "task manager": "taskmgr",
                    "paint": "mspaint",
                }
                exe = app_map.get(app_name, app_name)
                try:
                    subprocess.Popen([exe], shell=True)
                    return {"output": f"Opened {app_name}"}
                except Exception as e:
                    return {"error": f"Failed to open {app_name}: {e}"}

            elif tool == "remember":
                key = params.get("key", "")
                value = params.get("value", "")
                section = params.get("section", "Preferences")
                if key and value:
                    self.memory.t4_set(key.strip(), value.strip(), section.strip())
                    return {"output": f"Saved to your profile: {key} = {value}"}
                return {"error": "Both 'key' and 'value' are required"}

            elif tool == "set_reminder":
                text = params.get("text", "")
                when = params.get("when", "")
                category = params.get("category", "general")
                if not text or not when:
                    return {"error": "Both 'text' and 'when' are required"}
                try:
                    reminder_id = self.db.add_reminder(text, when, category)
                    return {"output": f"Reminder set: '{text}' at {when} (ID: {reminder_id})"}
                except Exception as e:
                    return {"error": f"Failed to set reminder: {e}"}

            elif tool == "list_reminders":
                include_fired = params.get("include_fired", "").lower() == "true"
                reminders = self.db.list_reminders(include_fired)
                if not reminders:
                    return {"output": "No reminders found."}
                lines = [f"- ID {r['id']}: {r['text']} (due: {r['due_at']})" for r in reminders]
                return {"output": "\n".join(lines)}

            elif tool == "delete_reminder":
                try:
                    rid = int(params.get("id", 0))
                    if self.db.delete_reminder(rid):
                        return {"output": f"Reminder {rid} deleted."}
                    return {"error": f"Reminder {rid} not found."}
                except (ValueError, TypeError):
                    return {"error": "Invalid reminder ID"}

            elif tool == "memory_save":
                tier = params.get("tier", "t4").lower()
                content = params.get("content", "")
                title = params.get("title", "")
                if not content:
                    return {"error": "'content' is required"}
                try:
                    if tier == "t3":
                        safe_title = title or content[:50]
                        path = self.memory.t3_save_episode(safe_title, content)
                        return {"output": f"Saved to T3 episodic memory: {path}"}
                    elif tier == "t4":
                        key = title or "saved_note"
                        self.memory.t4_set(key, content)
                        return {"output": f"Saved to T4 user profile: {key}"}
                    elif tier == "t5":
                        self.memory.t5_append(content, title or None)
                        return {"output": "Saved to T5 archive."}
                    return {"error": f"Unknown tier: {tier}. Use t3, t4, or t5."}
                except Exception as e:
                    return {"error": f"Memory save failed: {e}"}

            elif tool == "memory_search":
                query = params.get("query", "")
                tier = params.get("tier", "all").lower()
                if not query:
                    return {"error": "'query' is required"}
                results = []
                try:
                    if tier in ("t3", "all"):
                        t3_results = self.memory.t3_find_episodes(query, max_results=3)
                        for r in t3_results:
                            results.append(f"[T3] {r['title']} (score: {r['final_score']:.2f})")
                    if tier in ("t4", "all"):
                        t4_val = self.memory.t4_get(query)
                        if t4_val:
                            results.append(f"[T4] {query}: {t4_val}")
                    if tier in ("t5", "all"):
                        t5_results = self.memory.t5_search(query, max_results=3)
                        for r in t5_results:
                            results.append(f"[T5] {r['title']}: {r['snippet']}")
                    if not results:
                        return {"output": f"No memories found for '{query}'."}
                    return {"output": "\n".join(results)}
                except Exception as e:
                    return {"error": f"Memory search failed: {e}"}

            elif tool == "time":
                now = datetime.now()
                return {"output": now.strftime("%A, %B %d, %Y at %I:%M %p (%Z)")}

            elif tool == "weather":
                location = params.get("location", "")
                try:
                    import requests
                    loc = location or "auto"
                    url = f"https://wttr.in/{loc}?format=%l:+%C+%t,+%h+humidity,+%w+wind"
                    r = requests.get(url, timeout=10, headers={"User-Agent": "Alfred/1.0"})
                    if r.status_code == 200:
                        return {"output": r.text.strip()}
                    return {"error": f"Weather API returned {r.status_code}"}
                except Exception as e:
                    return {"error": f"Failed to get weather: {e}"}

            elif tool == "run_code":
                code = params.get("code", "")
                language = params.get("language", "python").lower()
                if not code:
                    return {"error": "'code' is required"}
                try:
                    if language == "shell":
                        result = subprocess.run(
                            ["powershell", "-Command", code],
                            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace"
                        )
                        output = result.stdout[:2000]
                        if result.stderr:
                            output += f"\nSTDERR: {result.stderr[:500]}"
                        if result.returncode != 0:
                            return {"error": f"Shell exited code {result.returncode}: {output[:500]}"}
                        return {"output": output.strip()}
                    else:
                        import tempfile
                        import textwrap
                        # Wrap user code in a safe execution context
                        wrapped = textwrap.dedent(f"""\
import sys, json, subprocess, os, re, math, random, datetime, pathlib
from pathlib import Path
try:
{textwrap.indent(code, '    ')}
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)
""")
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                            f.write(wrapped)
                            tmp_path = f.name
                        try:
                            result = subprocess.run(
                                [sys.executable, tmp_path],
                                capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace"
                            )
                            output = result.stdout[:2000]
                            if result.stderr:
                                error = result.stderr[:500]
                                if "ERROR:" in error:
                                    return {"error": error.split("ERROR:")[-1].strip()}
                            if result.returncode != 0:
                                return {"error": f"Python exited code {result.returncode}: {result.stderr[:300]}"}
                            return {"output": output.strip()}
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except Exception:
                                pass
                except Exception as e:
                    return {"error": f"Code execution failed: {e}"}

            return {"error": f"Tool {tool} not implemented"}

        except Exception as e:
            return {"error": str(e)}

    async def _web_search(self, query: str, num_results: int = 5) -> Dict:
        """Search the web via Exa API (primary) with DuckDuckGo fallback."""
        import re
        # Clean up query - strip task framing like "search", "find", "look up"
        clean = re.sub(r'^(search|find|look up|search for|find information about|look for)\s+', '', query, flags=re.IGNORECASE)
        # Strip "the web", "the internet", "the web for", "the internet for"
        clean = re.sub(r'^(the web|the internet|the web for|the internet for)\s+', '', clean, flags=re.IGNORECASE)
        # Strip trailing prepositions "for", "about", "on" left after cleaning
        clean = re.sub(r'^(for|about|on)\s+', '', clean, flags=re.IGNORECASE)
        # Strip trailing save/fetch/write instructions
        clean = re.sub(r'\s+(and\s+)?(save|write|fetch|download|store|save to file|save results|save it).*$', '', clean, flags=re.IGNORECASE)
        if clean.strip():
            query = clean.strip()
        query = query[:200]

        exa_key = os.environ.get("EXA_API_KEY", "")

        # Try Exa API first if key is available
        if exa_key:
            try:
                import requests
                resp = requests.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": exa_key, "Content-Type": "application/json"},
                    json={"query": query, "numResults": min(num_results, 10), "useAutoprompt": True, "text": True},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results", [])
                    if results:
                        output = "\n".join(
                            f"{i+1}. {r.get('title', 'No title')}\n   {r.get('url', '')}\n   {r.get('text', '')[:200]}"
                            for i, r in enumerate(results)
                        )
                        return {"output": output}
            except Exception as e:
                print(f"[Alfred] Exa search failed: {e}, falling back to DuckDuckGo")

        # DuckDuckGo fallback via HTML endpoint
        try:
            import httpx
            from bs4 import BeautifulSoup
            url = "https://html.duckduckgo.com/html/"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
                resp = await client.post(url, data={"q": query})
                soup = BeautifulSoup(resp.text, "html.parser")
                results = []
                for r in soup.select(".result"):
                    title_el = r.select_one(".result__title a")
                    snippet_el = r.select_one(".result__snippet")
                    if title_el:
                        results.append({
                            "title": title_el.get_text(strip=True),
                            "href": title_el.get("href", ""),
                            "body": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
                if results:
                    output = "\n".join(
                        f"{i+1}. {r['title']}\n   {r['href']}\n   {r['body'][:200]}"
                        for i, r in enumerate(results[:num_results])
                    )
                    return {"output": output}
            return {"output": "No results found"}
        except ImportError:
            return {"error": "httpx/bs4 not installed for web search"}
        except Exception as e:
            return {"error": str(e)}

    async def _web_fetch(self, url: str) -> Dict:
        """Fetch webpage content."""
        if not url or not url.startswith(("http://", "https://")):
            return {"error": f"Invalid URL: {url[:100]}. Must start with http:// or https://"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, follow_redirects=True)
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(separator="\n", strip=True)
                return {"output": text[:3000]}
        except Exception as e:
            return {"error": str(e)}

    # ============ VERIFICATION LOOP ============

    async def _verify_mutation(self, tool: str, params: Dict, output: str) -> bool:
        """Verify that a mutation tool actually changed the world.
        Returns True if the change appears to have succeeded, False otherwise."""
        try:
            if tool == "write_file":
                path = params.get("path", "")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        written = f.read()
                    content = params.get("content", "")
                    return len(written) >= len(content) * 0.9 and content[:100] in written
                except FileNotFoundError:
                    return False
            elif tool == "calendar":
                if "create" in str(params) or "Event created" in output or "created" in output.lower():
                    # Check if the specific event summary appears in the agenda
                    summary = params.get("summary", params.get("title", "")).lower()
                    # Extract a clean keyword from the summary (first 3 words)
                    keywords = [w for w in summary.split() if len(w) > 3][:3]
                    client = GWSClient()
                    agenda = client.get_agenda(days=7)
                    if not agenda or "error" in agenda.lower():
                        return False
                    agenda_lower = agenda.lower()
                    # Check if at least 2 keywords from the summary appear in the agenda
                    matches = sum(1 for kw in keywords if kw in agenda_lower)
                    return matches >= 2
                return True
            elif tool == "email":
                if "send" in str(params):
                    # For send, check the tool's actual output — not the LLM's generated text
                    # The tool returns "Email sent: ..." on success, or an error message
                    return "sent" in output.lower() and "error" not in output.lower()
                return True
            elif tool == "memory_save":
                return "Saved" in output or "saved" in output.lower()
            elif tool == "set_reminder":
                return "Reminder set" in output or "reminder set" in output.lower()
        except Exception as e:
            print(f"[Alfred] Verification error for {tool}: {e}", flush=True)
        return False  # Default: don't trust unless we can verify

    def _is_safe_path(self, path: str) -> bool:
        """Check if path is in a safe directory."""
        home = Path.home()
        safe_dirs = [
            str(home / "Coding"),
            str(home / "Documents"),
            str(home / "Downloads"),
            str(home / "Desktop"),
            str(home),
        ]
        abs_path = os.path.abspath(path)
        return any(abs_path.startswith(d) for d in safe_dirs if os.path.exists(d))

    async def _handle_error_smart(self, task: str, ctx: TaskContext, context: Dict = None) -> Optional[List[Dict]]:
        """Smart error recovery: use 70B model to find a completely different approach.
        Returns a new plan (list of steps) or None if recovery is impossible."""
        # Build context for the LLM — what we've tried and what went wrong
        tried_summary = "\n".join(
            f"  {i+1}. {a['tool']} — {a['error'][:200]}"
            for i, a in enumerate(ctx.tried_approaches)
        )

        system_prompt = f"""You are Alfred's error recovery system. A tool failed and you need to find a COMPLETELY DIFFERENT approach.

## Task
{task}

## What we've tried that failed:
{tried_summary}

## Instructions
1. Analyze WHY each attempt failed.
2. Propose a FUNDAMENTALLY DIFFERENT approach. Do NOT retry the same tool with the same params.
3. Use the `run_code` tool to write Python/shell scripts. This is your most powerful option.
 4. If the task needs Google Workspace, use the `calendar`, `email`, or `gws` tools directly.
5. Return a JSON array of steps (same format as planner).

Available tools:
{self._get_tool_descriptions()}

Return ONLY valid JSON array. Each step: {{"tool": "...", "description": "...", "params": {{...}}}}"""

        try:
            content = await self._call_llm(system_prompt, f"Find a new approach for: {task}", model=self.PLANNER_MODEL, max_tokens=800, temperature=0.5)
            if not content:
                return None

            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                plan = json.loads(json_match.group())
                if isinstance(plan, list) and len(plan) > 0:
                    # Verify it's actually different from what we tried
                    first = plan[0]
                    for tried in ctx.tried_approaches:
                        if first["tool"] == tried["tool"]:
                            same_params = all(first.get("params", {}).get(k) == tried.get("params", {}).get(k) for k in first.get("params", {}))
                            if same_params and not first.get("params", {}).get("code"):
                                # Same tool and params (no run_code) — likely retry, skip
                                return None
                    return plan
        except Exception as e:
            print(f"[Alfred] Smart recovery failed: {e}", flush=True)

        return None

    async def _format_response(self, task: str, ctx: TaskContext) -> str:
        """Format final response — pass raw tool results through LLM for human-friendly rephrasing."""
        if not ctx.execution_results:
            if ctx.tool_calls_made:
                tools_used = ", ".join(ctx.tool_calls_made)
                return f"Task completed using: {tools_used}"
            return f"Task '{task}' completed."

        raw_parts = []
        for r in ctx.execution_results:
            if r.result and r.result.strip():
                raw_parts.append(f"[{r.tool} result]:\n{r.result}")
            elif r.error:
                raw_parts.append(f"[{r.tool} error]: {r.error}")

        raw_output = "\n\n".join(raw_parts)

        try:
            formatted = await self._call_llm(
                "You are Alfred, Master Sam's AI assistant. Rephrase the raw tool output into a natural, concise answer. Never output raw URLs, IDs, timestamps, or tool names. Just answer as if you already knew it.",
                f"User asked: \"{task}\"\n\nRaw tool output:\n{raw_output}",
                model=self.CHAT_MODEL,
                max_tokens=400,
                temperature=0.5,
            )
            if formatted:
                return formatted
        except Exception as e:
            print(f"[Alfred] LLM response formatting failed: {e}", flush=True)

        # Fallback: concatenate results directly
        if len(raw_parts) == 1:
            return raw_parts[0]
        return "\n".join(raw_parts)


# Singleton
_alfred_instance: Optional[Alfred] = None

def get_alfred() -> Alfred:
    """Get singleton Alfred instance."""
    global _alfred_instance
    if _alfred_instance is None:
        _alfred_instance = Alfred()
    return _alfred_instance

async def execute_task(task: str, context: Dict = None) -> Dict[str, Any]:
    """Execute a task using Alfred."""
    alfred = get_alfred()
    return await alfred.execute(task, context)
