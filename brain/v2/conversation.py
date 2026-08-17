"""
Conversation Loop — Main LLM→tool→LLM execution loop.

Hermes-inspired:
    - Proper role alternation (user → assistant → tool → assistant).
    - PromptBuilder assembles system prompt under token budget.
    - ConversationHistory tracks tokens and compresses when needed.
    - ToolExecutor handles dispatch with guardrails and validation.
    - After mutation tools, automatic read-back verification is injected.
    - Max-turns limit prevents infinite loops.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from .prompt_builder import PromptBuilder, ToolSchema, count_tokens, PRIO_IDENTITY, PRIO_RULES, PRIO_PROFILE
from .context_manager import ConversationHistory
from .tool_executor import ToolExecutor, ToolResult, create_tool_executor, MUTATION_TOOLS, VERIFY_MAP


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class Response:
    """Structured response from the conversation loop."""
    response: str
    thinking: List[str] = field(default_factory=list)
    tools_called: List[str] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    episodes_saved: int = 0


# ---------------------------------------------------------------------------
# Alfred — the main agent class
# ---------------------------------------------------------------------------

class Alfred:
    """
    Alfred v2: Hermes-inspired clean agent loop.

    Public API:
        execute(task, context) -> Response
        pop_alerts() -> List[Dict]
        check_due_reminders() -> List[Dict]
    """

    CHAT_MODEL = "llama-3.1-8b-instant"
    MAX_TURNS = 10
    CONVERSATION_BUDGET = 12000
    SYSTEM_PROMPT_BUDGET = 8000

    _BOOTSTRAP_DIR = Path(
        os.environ.get("OBSIDIAN_VAULT_PATH",
                       r"C:\Coding\notes idk obsidian\Aflred-brain")
    )

    def __init__(self) -> None:
        from ..memory.five_tier import get_memory
        from ..memory.skill_manager import get_skill_manager
        from ..local_db import get_local_db
        from ..llm_router import LLMRouter

        self.memory = get_memory()
        self.skill_manager = get_skill_manager()
        self.db = get_local_db()
        self.heartbeat_enabled = True
        self._pending_alerts: List[Dict] = []

        # LLM router (3-provider fallback)
        groq_key = os.environ.get("GROQ_API_KEY", "")
        google_key = os.environ.get("GOOGLE_API_KEY", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._router = LLMRouter(
            groq_key=groq_key, gemini_key=google_key,
            openrouter_key=openrouter_key,
        )

        # Hermes-inspired components
        self._prompt_builder = PromptBuilder(token_budget=self.SYSTEM_PROMPT_BUDGET)
        self._tool_executor = create_tool_executor()

        # Bootstrap (AGENTS.md, SOUL.md, etc.)
        self._bootstrap = self._load_bootstrap()

        # Start heartbeat
        self._start_heartbeat()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _load_bootstrap(self) -> Dict[str, str]:
        out = {}
        for name in ["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md"]:
            p = self._BOOTSTRAP_DIR / name
            try:
                if p.exists():
                    out[name] = p.read_text(encoding="utf-8")
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------
    # System prompt assembly (via PromptBuilder)
    # ------------------------------------------------------------------

    def _build_system_prompt(self, memory_snippets: Optional[List[str]] = None) -> str:
        """
        Build the system prompt using PromptBuilder.

        Priority: Identity > Rules > Profile > Tools > Skills > Memory
        """
        # Identity
        identity = "You are Alfred, Master Sam's autonomous AI assistant."

        # Rules
        rules = [
            "For personal questions about Master Sam, answer from the profile below. NEVER search memory for it.",
            "For live data (time, weather, calendar, web), call the appropriate tool.",
            "Output ONE JSON per message: {\"tool\": \"name\", \"params\": {...}} or {\"reply\": \"answer\"}",
            "After a tool runs you will see the result. Call another tool or reply.",
            "NEVER describe what you will do — just call the tool or reply.",
            "NEVER claim an action was completed without calling the tool and seeing a success result.",
            "CRITICAL: calendar, email, weather, time answers REQUIRE a tool call. Never answer from memory.",
            "Translate raw tool output into natural language — never dump JSON, IDs, or technical errors.",
            "Self-correct on failure. If a tool errors, explain why in plain terms.",
            "If user corrects you mid-query, acknowledge the correction and redo with the correct information.",
        ]
        soul = self._bootstrap.get("SOUL.md", "")
        if soul:
            rules.append("Tone: confident, witty, direct. No sycophancy or robotic language.")

        # Profile (T4)
        profile = ""
        try:
            t4 = self.memory.get_context_for_llm()
            if t4 and "User Profile" in t4:
                profile = t4.split("## User Profile:")[-1].strip()[:2000]
        except Exception:
            pass

        # Tools
        tool_schemas = []
        for name, desc_dict in self._get_tool_descriptions().items():
            tool_schemas.append(ToolSchema(
                name=name,
                description=desc_dict.get("description", ""),
                parameters=desc_dict.get("params", {}),
            ))

        # Skills (T2)
        skills = []
        try:
            for s in self.memory._t2_skills_index[:5]:
                skills.append({
                    "title": s.get("title", ""),
                    "description": s.get("description", ""),
                    "steps": s.get("steps", []),
                })
        except Exception:
            pass

        # Assemble
        result = self._prompt_builder.assemble(
            identity=identity,
            profile=profile,
            tools=tool_schemas,
            skills=skills,
            memory=memory_snippets,
            rules=rules,
        )

        return result.system

    def _get_tool_descriptions(self) -> Dict[str, Dict[str, Any]]:
        """Get tool descriptions for prompt injection."""
        return {
            "chat": {
                "description": "Have a conversation or answer a question.",
                "params": {"message": "Message to respond to"},
            },
            "calculator": {
                "description": "Evaluate a math expression safely.",
                "params": {"expression": "Expression like '2+2'"},
            },
            "calendar": {
                "description": "List/create Google Calendar events. Actions: agenda, list, create.",
                "params": {
                    "action": "agenda/list/create",
                    "summary": "Title (for create)",
                    "start_time": "ISO datetime (for create)",
                    "end_time": "ISO datetime (optional, auto +1hr)",
                },
            },
            "email": {
                "description": "Check/send Gmail. Actions: triage, list, read, send.",
                "params": {
                    "action": "triage/list/read/send",
                    "to": "Recipient (for send)",
                    "subject": "Subject (for send)",
                    "body": "Body (for send)",
                },
            },
            "web_search": {
                "description": "Search the web.",
                "params": {"query": "Search query", "num_results": "Results count"},
            },
            "web_fetch": {
                "description": "Read a webpage.",
                "params": {"url": "URL to fetch"},
            },
            "shell": {
                "description": "Run a PowerShell command.",
                "params": {"command": "Command", "cwd": "Working dir (optional)"},
            },
            "read_file": {
                "description": "Read a file from safe directories.",
                "params": {"path": "File path", "offset": 1, "limit": 2000},
            },
            "write_file": {
                "description": "Write content to a file.",
                "params": {"path": "File path", "content": "Content"},
            },
            "list_directory": {
                "description": "List files in a directory.",
                "params": {"path": "Directory path"},
            },
            "glob": {
                "description": "Find files by glob pattern.",
                "params": {"pattern": "**/*.py"},
            },
            "screenshot": {
                "description": "Take a screenshot.",
                "params": {},
            },
            "open_app": {
                "description": "Open a desktop app.",
                "params": {"app_name": "App name"},
            },
            "gws": {
                "description": "Run Google Workspace commands beyond calendar/email.",
                "params": {"command": "string"},
            },
            "time": {
                "description": "Get current date and time.",
                "params": {},
            },
            "remember": {
                "description": "Save a fact to long-term profile (T4).",
                "params": {"key": "Fact name", "value": "Fact value",
                           "section": "Category (default: Preferences)"},
            },
            "set_reminder": {
                "description": "Set a reminder.",
                "params": {"text": "Reminder text",
                           "when": "ISO datetime or natural language",
                           "category": "Category (default: general)"},
            },
            "list_reminders": {
                "description": "List pending reminders.",
                "params": {"include_fired": "true/false"},
            },
            "delete_reminder": {
                "description": "Delete a reminder by ID.",
                "params": {"id": "Reminder ID"},
            },
            "memory_save": {
                "description": "Save to memory (T3/T4/T5).",
                "params": {"tier": "t3/t4/t5", "content": "Content",
                           "title": "Optional title"},
            },
            "memory_search": {
                "description": "Search memory tiers.",
                "params": {"query": "Query", "tier": "all/t3/t4/t5"},
            },
            "weather": {
                "description": "Get weather for a location.",
                "params": {"location": "City or 'auto'"},
            },
            "run_code": {
                "description": "Execute Python or PowerShell safely.",
                "params": {"code": "Code", "language": "python/shell"},
            },
        }

    # ------------------------------------------------------------------
    # LLM output parser
    # ------------------------------------------------------------------

    def _parse_llm_output(self, content: str) -> tuple:
        """
        Parse LLM output into (reply, tool_name, tool_params).

        Returns:
            (reply_str, None, None) if reply
            (None, tool_name, tool_params) if tool call
            (text, None, None) if unparseable (treated as reply)
        """
        text = content.strip()
        json_objects = []
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
                        json_objects.append(json.loads(text[start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = -1

        if json_objects:
            # Prefer reply over tool call
            for obj in json_objects:
                if "reply" in obj:
                    return (str(obj["reply"])[:500], None, None)
            # Otherwise use first tool call
            for obj in json_objects:
                if "tool" in obj:
                    params = obj.get("params", {})
                    # Handle nested tool calls
                    if isinstance(params, dict) and "tool" in params:
                        params = params.get("params", {})
                    return (None, obj["tool"], params)

        # Fallback: brace scanning
        first_brace = text.find("{")
        if first_brace >= 0:
            for end in range(first_brace + 1, min(len(text), first_brace + 600)):
                if text[end] == "}":
                    try:
                        obj = json.loads(text[first_brace:end + 1])
                        if isinstance(obj, dict):
                            if "reply" in obj:
                                return (str(obj["reply"])[:500], None, None)
                            if "tool" in obj:
                                params = obj.get("params", {})
                                if isinstance(params, dict) and "tool" in params:
                                    params = params.get("params", {})
                                return (None, obj["tool"], params)
                    except json.JSONDecodeError:
                        continue

        return (text[:500], None, None)

    # ------------------------------------------------------------------
    # Main execute loop
    # ------------------------------------------------------------------

    async def execute(self, task: str, context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Execute a task through the Hermes-inspired conversation loop.

        Loop:
            1. Build system prompt (PromptBuilder).
            2. Call LLM.
            3. Parse response → reply or tool call.
            4. If tool call → execute → add result → compress if needed.
            5. If reply → return.
            6. Max turns = 10.
        """
        thinking: List[str] = []
        history = context.get("conversation_history", []) if context else []

        # Build context for tool handlers
        tool_ctx = {
            "memory": self.memory,
            "db": self.db,
            "router": self._router,
            "bootstrap": self._bootstrap,
        }

        # Initialize conversation history
        conv = ConversationHistory(token_budget=self.CONVERSATION_BUDGET)
        if history:
            conv.seed_from_history(history)

        # Add user message
        conv.add_user(task)

        final_reply = ""
        tools_called: List[str] = []
        tool_results: List[Dict[str, Any]] = []

        for turn in range(self.MAX_TURNS):
            # --- Build prompt ---
            # Get memory snippets for prompt
            memory_snippets = self._get_memory_snippets(task)
            system = self._build_system_prompt(memory_snippets)

            # Convert conversation to LLM format
            llm_messages = conv.to_llm_messages()

            # --- Call LLM ---
            resp = await self._router.call(
                system_prompt=system,
                user_message=task,
                messages=llm_messages,
                max_tokens=1200,
                temperature=0.1,
            )
            if resp.provider:
                note = f"[LLM provider={resp.provider}"
                if resp.fallback_used:
                    note += f", fallback={resp.fallback_reason or 'unknown'}"
                thinking.append(note + "]")
            raw = (resp.text or "").strip()

            # Retry with stripped prompt if empty
            if not raw:
                raw = await self._retry_with_stripped_prompt(task)

            # --- Parse output ---
            reply, tool_name, tool_params = self._parse_llm_output(raw)

            if reply is not None:
                if reply.strip():
                    final_reply = reply
                    thinking.append(f"[Turn {turn + 1}] Reply: {reply[:80]}")
                    break
                # Empty reply — nudge LLM
                thinking.append(f"[Turn {turn + 1}] Empty reply, retrying")
                conv.add_user(f"You replied with an empty message. Task: {task}. Respond properly.")
                continue

            if tool_name is None:
                final_reply = "I'm not sure how to handle that."
                thinking.append(f"[Turn {turn + 1}] Unparseable: {raw[:60]}")
                break

            # --- Execute tool ---
            thinking.append(f"[Turn {turn + 1}] Tool: {tool_name}")
            result = await self._tool_executor.execute(tool_name, tool_params, tool_ctx)

            output = result.output if result.success else result.error or ""
            is_error = not result.success

            tools_called.append(tool_name)
            tool_results.append({
                "tool": tool_name,
                "output": str(output)[:500],
                "success": result.success,
            })

            if is_error:
                thinking.append(f"  X {tool_name}: {str(output)[:100]}")
            else:
                thinking.append(f"  OK ({len(str(output))} chars)")

            # --- Add tool result to conversation ---
            result_dict = result.to_dict()
            conv.add_tool_result(tool_name, result_dict)

            # --- Mutation verification ---
            if result.success and tool_name in MUTATION_TOOLS:
                verify_result = await self._tool_executor.verify_mutation(tool_name, tool_ctx)
                if verify_result:
                    verify_output = verify_result.output if verify_result.success else verify_result.error
                    conv.add_tool_result(
                        f"{tool_name}_verify",
                        {"verification": str(verify_output)[:500]},
                    )
                    thinking.append(f"  Verified: {tool_name}")

            # --- Compress if needed ---
            compressed = conv.compress_if_needed()
            if compressed:
                thinking.append(f"  Compressed {compressed} old messages")

            # --- Next turn context ---
            # The conversation history already has the tool result,
            # so the next LLM call will see it naturally.

        # --- Fallback if no reply ---
        if not final_reply:
            if tools_called:
                final_reply = (
                    f"Task completed using {len(tools_called)} tool(s): "
                    f"{', '.join(tools_called)}."
                )
            else:
                final_reply = "I wasn't able to process that request."

        # --- Save T3 episode ---
        episodes_saved = 0
        if any(t != "chat" for t in tools_called):
            try:
                tools_used = ", ".join(tools_called)
                summary = (
                    f"## User Request\n{task}\n\n"
                    f"## Tools Used\n{tools_used}\n\n"
                    f"## Alfred Response\n{final_reply}"
                )
                self.memory.t3_save_episode(title=task[:80], content=summary[:2000])
                thinking.append("Saved to T3 episodic memory")
                episodes_saved = 1
            except Exception:
                pass

        # --- Maybe generate skill ---
        if len(tools_called) >= 3 and not any(
            tr.get("success") is False for tr in tool_results
        ):
            try:
                self._maybe_generate_skill(task, tools_called)
            except Exception:
                pass

        return {
            "response": final_reply,
            "thinking": thinking,
            "tools_called": tools_called,
            "tool_results": tool_results,
            "episodes_saved": episodes_saved,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_memory_snippets(self, task: str) -> List[str]:
        """Get T3 episodic memory snippets relevant to the task."""
        snippets: List[str] = []
        try:
            results = self.memory.t3_find_episodes(task, max_results=2)
            for r in results:
                try:
                    content = Path(r["path"]).read_text(encoding="utf-8")[:500]
                    snippets.append(f"### {r['title']}\n{content}")
                except Exception:
                    pass
        except Exception:
            pass
        return snippets

    async def _retry_with_stripped_prompt(self, task: str) -> str:
        """Retry with a minimal system prompt if the full prompt fails."""
        stripped = "You are Alfred, Master Sam's assistant.\n"
        stripped += "Output ONE JSON: {\"reply\": \"answer\"} or {\"tool\": \"name\", \"params\": {...}}\n"
        try:
            t4 = self.memory.get_context_for_llm()
            if t4 and "User Profile" in t4:
                stripped += "\n## PROFILE\n" + t4.split("## User Profile:")[-1].strip()[:500]
        except Exception:
            pass
        descs = "\n".join(
            f"- {n}: {d['description']}"
            for n, d in self._get_tool_descriptions().items()
        )
        stripped += "\n## TOOLS\n" + descs
        try:
            resp = await self._router.call(
                system_prompt=stripped,
                user_message=task,
                messages=[],
                max_tokens=800,
                temperature=0.1,
            )
            return (resp.text or "").strip()
        except Exception:
            return ""

    def _maybe_generate_skill(self, task: str, tools: List[str]) -> None:
        """Auto-generate a skill for complex multi-tool tasks."""
        self.skill_manager.create_skill(
            title=f"v2-auto-{task[:40]}",
            description=f"Auto-generated from task: {task[:100]}",
            steps=tools,
            category="auto",
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the CognitiveHeartbeat background loop."""
        from .heartbeat import CognitiveHeartbeat
        self._heartbeat = CognitiveHeartbeat(
            alfred=self,
            db=self.db,
            router=self._router,
            memory=self.memory,
            interval=1800,
        )
        self._heartbeat.start()

    def stop(self) -> None:
        """Stop the background heartbeat (called on interpreter shutdown)."""
        if getattr(self, "_heartbeat", None):
            self._heartbeat.stop()

    def check_due_reminders(self) -> List[Dict]:
        """Check for due reminders and queue alerts."""
        try:
            due = self.db.get_due_reminders()
            alerts = []
            for r in due:
                self.db.mark_reminder_fired(r["id"])
                a = {
                    "type": "reminder",
                    "text": r["text"],
                    "id": r["id"],
                    "category": r.get("category", "general"),
                }
                alerts.append(a)
                self._pending_alerts.append(a)
            return alerts
        except Exception as e:
            print(f"[Alfred-v2] Reminder check failed: {e}")
            return []

    def pop_alerts(self) -> List[Dict]:
        """Pop pending alerts for WebSocket broadcast."""
        alerts = list(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts


# ---------------------------------------------------------------------------
# Public API (preserves existing interface for brain_api/server.py)
# ---------------------------------------------------------------------------

_alfred_instance: Optional[Alfred] = None


def get_alfred() -> Alfred:
    """Return the global Alfred v2 singleton."""
    global _alfred_instance
    if _alfred_instance is None:
        _alfred_instance = Alfred()
    return _alfred_instance


async def execute_task(task: str, context: Optional[Dict] = None) -> Dict[str, Any]:
    """Convenience async wrapper."""
    alfred = get_alfred()
    return await alfred.execute(task, context)
