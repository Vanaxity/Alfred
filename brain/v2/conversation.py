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
        from ..goal_inference import get_goal_expander

        self.memory = get_memory()
        self.skill_manager = get_skill_manager()
        self.goal_expander = get_goal_expander()
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

    def _build_system_prompt(
        self,
        memory_snippets: Optional[List[str]] = None,
        matched_skill: Optional[Any] = None,
    ) -> "tuple[str, List[str]]":
        """
        Build the system prompt using PromptBuilder.

        Priority: Identity > Rules > Profile > Tools > Memory > Skills

        Returns (system_text, dropped_section_names) — the latter surfaces
        sections that lost the token-budget fitting entirely (not merely
        truncated), so a caller can log when memory silently vanished
        instead of that being invisible.
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
            "CRITICAL: ALL arithmetic, trigonometry, and geometry REQUIRE the calculator tool — never compute mentally and never state a number you did not get from it. Word problems count: extract the expression and call calculator.",
            "If the question contains a contradiction or impossible premise (e.g. a right triangle whose leg exceeds its hypotenuse, a date that doesn't exist), SAY SO and stop. Never silently reinterpret the numbers into something solvable — for homework, a confident answer to a broken question is worse than no answer.",
            "If a required detail is genuinely missing (a time, a name, a value), ASK for it. Never invent it and never quietly assume a default.",
            "Before you reply that something is done, check you finished EVERY part of it. If a request had two parts and you did one, say exactly which part is still outstanding — never describe a half-finished action as complete.",
            "Prefer calculator over run_code for anything calculator supports; run_code needs approval and is slower.",
            "Translate raw tool output into natural language — never dump JSON, IDs, or technical errors.",
            "NEVER use LaTeX or backslash commands in your reply. Write math in plain text: 'x^2' not '\\(x^2\\)', '1/2' not '\\frac{1}{2}', 'theta' not '\\theta'. Backslashes corrupt the JSON envelope and the UI shows plain text anyway.",
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

        # Skills (T2) — a specifically-matched skill for this task, not an
        # arbitrary unranked "first 5" (the old behavior had no relevance
        # filtering at all: it showed whatever happened to be first in the
        # index regardless of the task, which is noise, not guidance).
        skills = []
        if matched_skill is not None:
            skills.append({
                "title": matched_skill.title,
                "description": matched_skill.description,
                "steps": matched_skill.steps,
            })

        # Assemble
        result = self._prompt_builder.assemble(
            identity=identity,
            profile=profile,
            tools=tool_schemas,
            skills=skills,
            memory=memory_snippets,
            rules=rules,
        )

        return result.system, result.dropped_sections

    def _get_tool_descriptions(self) -> Dict[str, Dict[str, Any]]:
        """Get tool descriptions for prompt injection."""
        return {
            "chat": {
                "description": "Have a conversation or answer a question.",
                "params": {"message": "Message to respond to"},
            },
            "calculator": {
                "description": (
                    "Evaluate a math expression. Supports arithmetic, powers, and "
                    "functions: sin cos tan asin acos atan atan2 sqrt cbrt log log2 "
                    "log10 exp degrees radians abs round floor ceil min max pow hypot "
                    "factorial, plus constants pi/e/tau. Trig takes RADIANS — convert "
                    "degrees with radians(x) or x*pi/180. USE THIS for any arithmetic, "
                    "trigonometry, or geometry, including word problems; do NOT use "
                    "run_code for math this can do."
                ),
                "params": {
                    "expression": "e.g. '2+2', '47*tan(radians(35))', 'sqrt(144)'"
                },
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
                "description": (
                    "Create a reminder, or CHANGE an existing one. To reschedule or "
                    "correct a reminder, call this with its 'id' and the new 'when' "
                    "— that updates it in one step. Do NOT delete-then-recreate: "
                    "deleting alone leaves the user with no reminder at all."
                ),
                "params": {"text": "Reminder text",
                           "when": "ISO datetime or natural language",
                           "id": "Existing reminder ID — only when changing one",
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

    @staticmethod
    def _loads_lenient(chunk: str) -> Optional[Any]:
        """json.loads, retrying once with invalid backslash escapes repaired.

        Models writing math reply in LaTeX -- "the derivative of \\(x^2\\)" --
        which is not valid JSON, since \\( and \\) aren't legal escapes. Strict
        json.loads rejects the whole object, the reply can't be extracted, and
        the user is shown the raw JSON envelope instead of the answer. That hits
        essentially every math explanation, so repair rather than lose it.
        """
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
        # Escape any backslash not starting a valid JSON escape (" \ / b f n r t u).
        repaired = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", chunk)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None

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
                    obj = self._loads_lenient(text[start:i + 1])
                    if obj is not None:
                        json_objects.append(obj)
                    start = -1

        if json_objects:
            # Prefer a TOOL CALL over a reply when the model emits both.
            #
            # This used to be the other way round ("prefer reply over tool
            # call"), which is backwards: models routinely narrate before
            # acting -- {"reply": "Let me calculate that..."} followed by
            # {"tool": "calculator", ...} -- and taking the narration meant the
            # tool never ran and the narration became the final answer. That is
            # a direct path to a confident, ungrounded number (the live "53
            # feet" answer to a trig question whose real answer was 33).
            # A tool call produces grounded data; narration does not.
            for obj in json_objects:
                if "tool" in obj:
                    as_reply = self._reply_shaped_tool(obj)
                    if as_reply is not None:
                        return (as_reply, None, None)
                    return (None, obj["tool"], self._extract_params(obj))
            for obj in json_objects:
                if "reply" in obj:
                    return (str(obj["reply"]), None, None)

        # Fallback: brace scanning
        first_brace = text.find("{")
        if first_brace >= 0:
            for end in range(first_brace + 1, min(len(text), first_brace + 600)):
                if text[end] == "}":
                    obj = self._loads_lenient(text[first_brace:end + 1])
                    if isinstance(obj, dict):
                        if "reply" in obj and "tool" not in obj:
                            return (str(obj["reply"]), None, None)
                        if "tool" in obj:
                            as_reply = self._reply_shaped_tool(obj)
                            if as_reply is not None:
                                return (as_reply, None, None)
                            return (None, obj["tool"], self._extract_params(obj))

        # Unparseable. Never hand a raw JSON envelope to the user — a model that
        # emits truncated or malformed JSON (seen live: an unclosed brace when
        # output hit the token cap) would otherwise surface as
        # '{ "tool": "calculator", "expression": ...' in the chat window.
        if text.lstrip().startswith(("{", "[")):
            return (
                "I got a malformed response from the model on that one. "
                "Could you ask again?",
                None,
                None,
            )
        return (text[:500], None, None)

    @staticmethod
    def _reply_shaped_tool(obj: Dict[str, Any]) -> Optional[str]:
        """Detect a reply the model dressed up as a tool call.

        There is no `reply` tool, but models emit {"tool": "reply", "params":
        {"message": "..."}} anyway — mixing up the two shapes in the protocol.
        Seen live: a run where 'reply' appears in tools_called, meaning the
        executor was asked to run a tool that doesn't exist, the turn was
        wasted, and the loop kept going until it hit MAX_TURNS.

        Returns the reply text, or None if this really is a tool call.
        """
        name = str(obj.get("tool", "")).strip().lower()
        if name not in ("reply", "respond", "answer", "final_answer", "response"):
            return None
        params = obj.get("params")
        if isinstance(params, dict):
            for key in ("reply", "message", "text", "content", "answer", "response"):
                val = params.get(key)
                if isinstance(val, str) and val.strip():
                    return val[:500]
            # Single unnamed string param — take it rather than lose the answer.
            strings = [v for v in params.values() if isinstance(v, str) and v.strip()]
            if len(strings) == 1:
                return strings[0][:500]
        elif isinstance(params, str) and params.strip():
            return params[:500]
        for key in ("reply", "message", "text", "content"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val[:500]
        return None

    @staticmethod
    def _extract_params(obj: Dict[str, Any]) -> Dict[str, Any]:
        """Pull tool params out of a parsed tool-call object.

        Tolerates the shapes models actually emit, not just the documented one:
          {"tool": t, "params": {...}}            — the contract
          {"tool": t, "params": {"tool":…, "params":{...}}} — nested duplication
          {"tool": t, "expression": "2+2"}        — params inlined at top level
        The last was seen live: a model wrote `{"tool":"calculator",
        "expression":"..."}`, which used to yield empty params and silently run
        the tool against nothing.
        """
        params = obj.get("params")
        if isinstance(params, dict):
            # Nested tool call: unwrap one level.
            if "tool" in params:
                inner = params.get("params")
                return inner if isinstance(inner, dict) else {}
            return params
        # No usable "params" key — treat any other top-level keys as the params.
        inlined = {k: v for k, v in obj.items() if k not in ("tool", "params", "reply")}
        return inlined

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
            "approved_actions": (context or {}).get("approved_actions") or [],
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
        awaiting_approval: Optional[Dict[str, Any]] = None
        repeats: Dict[str, int] = {}

        # --- Goal expansion + skill matching (once, before the loop) ---
        # Expansion must happen before matching, since matching is meant to
        # consume the clarified intent ("bro I have tests every Monday" ->
        # "Prepare and study consistently...") rather than raw casual phrasing
        # a title-similarity/word-overlap matcher would score poorly against.
        # Only .expanded feeds skill-matching; conv.add_user()/task above and
        # T3 episode titles below keep using literal `task` so history stays
        # human-readable rather than showing Alfred's paraphrase of the user.
        planning_text = task
        try:
            expanded = await self.goal_expander.expand(task)
            planning_text = expanded.expanded or task
        except Exception:
            pass

        matched_skill = None
        try:
            # search_ecosystem=False: this runs on every task, and the
            # ecosystem path can shell out to npx and install files with a
            # 60s timeout -- fine for an explicit "find me a skill" action,
            # not as a side effect of ordinary conversation turns.
            matched_skill = self.skill_manager.find_skill(
                planning_text, search_ecosystem=False
            )
        except Exception:
            pass

        # --- T3 memory snippets (once, before the loop) ---
        # task is fixed for the whole call, so recomputing this every turn was
        # a redundant embedding/DB round trip with an identical result each time.
        memory_snippets = self._get_memory_snippets(task)
        memory_drop_logged = False

        for turn in range(self.MAX_TURNS):
            # --- Build prompt ---
            system, dropped_sections = self._build_system_prompt(memory_snippets, matched_skill)
            if "memory" in dropped_sections and not memory_drop_logged:
                thinking.append(
                    "  Memory dropped from prompt (token budget too tight to fit it)"
                )
                memory_drop_logged = True

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

            # Record the LLM's own output into history. Message.is_tool_call is
            # computed from content (does it parse as JSON with a "tool" key?),
            # so recording raw here — before branching on what it turned out to
            # be — makes ConversationHistory._find_last_tool_pair_start() able
            # to recognize a tool-call turn automatically. Without this, the
            # ASSISTANT message a TOOL result depends on never existed, so
            # compression could never identify a pair to preserve.
            if raw:
                conv.add_assistant(raw)

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

            # --- Break out of repeat loops ---
            # Seen live: five identical memory_search calls in a row, burning
            # every turn and ending in the MAX_TURNS fallback. Re-running the
            # exact same call cannot produce a different result, so tell the
            # model plainly instead of letting it spin.
            call_sig = f"{tool_name}:{json.dumps(tool_params, sort_keys=True, default=str)}"
            repeats[call_sig] = repeats.get(call_sig, 0) + 1
            if repeats[call_sig] > 2:
                thinking.append(f"[Turn {turn + 1}] Aborting repeat of {tool_name}")
                conv.add_user(
                    f"You have already called {tool_name} with those exact arguments "
                    f"{repeats[call_sig] - 1} times and got the same result. Do not call "
                    "it again. Either answer with what you have, or say what specific "
                    "information you are missing."
                )
                continue

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
                "params": tool_params,
            })

            if is_error:
                thinking.append(f"  X {tool_name}: {str(output)[:100]}")
            else:
                thinking.append(f"  OK ({len(str(output))} chars)")

            # --- Add tool result to conversation ---
            result_dict = result.to_dict()
            conv.add_tool_result(tool_name, result_dict)

            # --- Stop immediately if the tool needs approval ---
            # Don't burn the remaining turns retrying a call that's blocked on a
            # human decision, not a fixable error — the LLM has no params
            # correction that gets past "a human hasn't said yes yet." The
            # attempt is already recorded above (add_assistant + add_tool_result),
            # so a resend with approved_actions set will have full context.
            if result.metadata.get("awaiting_approval"):
                awaiting_approval = {
                    "tool": result.metadata.get("tool", tool_name),
                    "params": result.metadata.get("params"),
                    "signature": result.metadata.get("signature"),
                }
                final_reply = (
                    f"I need your approval before running {tool_name}. "
                    "Confirm and I'll proceed."
                )
                thinking.append(f"  Awaiting approval: {tool_name}")
                break

            # --- Mutation verification ---
            # is_mutation() is action-aware: a calendar "agenda" read is not a
            # mutation even though "calendar" is in MUTATION_TOOLS.
            if result.success and self._tool_executor.is_mutation(tool_name, tool_params):
                verify_result = await self._tool_executor.verify_mutation(
                    tool_name, tool_ctx, tool_params
                )
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
        # The loop ran out of turns without the model producing an answer. The
        # old text here listed internal tool names at the user ("Task completed
        # using 10 tool(s): memory_search, memory_search, ..."), which leaks
        # implementation detail and, worse, claims completion for something that
        # did not complete. Say what actually happened instead.
        if not final_reply:
            if tools_called:
                last = tool_results[-1] if tool_results else None
                if last and last.get("success") and last.get("output"):
                    final_reply = (
                        "I ran out of steps before I could summarize that properly. "
                        f"Here's the last thing I got back:\n\n{str(last['output'])[:400]}"
                    )
                else:
                    final_reply = (
                        "I wasn't able to finish that — I kept working but never "
                        "reached an answer. Could you narrow it down or give me the "
                        "missing detail?"
                    )
            else:
                final_reply = "I wasn't able to process that request."

        # --- Save T3 episode ---
        episodes_saved = 0
        episode_path: Optional[str] = None
        if any(t != "chat" for t in tools_called):
            try:
                tools_used = ", ".join(tools_called)
                summary = (
                    f"## User Request\n{task}\n\n"
                    f"## Tools Used\n{tools_used}\n\n"
                    f"## Alfred Response\n{final_reply}"
                )
                episode_path = self.memory.t3_save_episode(title=task[:80], content=summary[:2000])
                thinking.append("Saved to T3 episodic memory")
                episodes_saved = 1
            except Exception:
                pass

        # --- Maybe generate skill ---
        skill_generated = False
        if len(tools_called) >= 3 and not any(
            tr.get("success") is False for tr in tool_results
        ):
            try:
                skill_generated = self._maybe_generate_skill(task, tool_results)
            except Exception as e:
                thinking.append(f"  Skill generation failed: {e}")

        return {
            "response": final_reply,
            "thinking": thinking,
            "tools_called": tools_called,
            "tool_results": tool_results,
            "episodes_saved": episodes_saved,
            "episode_path": episode_path,
            "skill_used": matched_skill is not None,
            "skill_generated": skill_generated,
            "awaiting_approval": awaiting_approval,
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

    def _maybe_generate_skill(self, task: str, tool_results: List[Dict[str, Any]]) -> bool:
        """Auto-generate a skill for complex multi-tool tasks.

        Was previously calling SkillManager.create_skill(), a method that does
        not exist — every call threw AttributeError, silently swallowed by a
        bare except, so skills were never actually generated on this path.
        generate_skill() needs steps as List[Dict] with real params to be worth
        anything when replayed, not just the tool names — tool_results already
        carries params per call, so build proper steps from it.
        """
        steps = [
            {
                "tool": tr["tool"],
                "description": f"Call {tr['tool']}",
                "params": tr.get("params") or {},
            }
            for tr in tool_results
            if tr.get("success")
        ]
        if not steps:
            return False
        complexity = "complex" if len(steps) >= 5 else "moderate"
        skill = self.skill_manager.generate_skill(
            task=task, steps=steps, task_complexity=complexity, had_error=False,
        )
        return skill is not None

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
                a = {
                    "type": "reminder",
                    "text": r["text"],
                    "id": r["id"],
                    "category": r.get("category", "general"),
                }
                alerts.append(a)
                self.push_alert(a)
                self.db.mark_reminder_fired(r["id"])
            return alerts
        except Exception as e:
            print(f"[Alfred-v2] Reminder check failed: {e}")
            return []

    def push_alert(self, alert: Dict[str, Any]) -> None:
        """Queue an alert for the WebSocket broadcaster to pick up.

        This is the single alert entry point — brain_api/server.py's
        _alert_broadcaster() polls pop_alerts() below every 5s. Previously
        CognitiveHeartbeat kept its own separate _pending_alerts that nothing
        ever drained, so every reminder/cron/proactive alert it generated
        vanished silently; it now calls this instead.
        """
        self._pending_alerts.append(alert)

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
