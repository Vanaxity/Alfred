# Architecture Overview — Alfred v2

This document explains the modular design of Alfred v2, a Hermes-inspired clean agent loop.

---

## 🔄 Main Execution Loop

```
User Task
    ↓
[1] Build System Prompt (PromptBuilder)
    ├─ Identity: "You are Alfred..."
    ├─ Rules: Tool format, guardrails
    ├─ Profile: T4 user profile
    ├─ Tools: Available tool schemas
    ├─ Skills: Recent T2 skills
    └─ Memory: T3 episodic snippets
    ↓
[2] Call LLM (LLMRouter)
    ├─ Primary: Groq (fast, cheap)
    ├─ Fallback 1: OpenRouter (if timeout/rate-limit)
    └─ Fallback 2: Gemini (if both fail)
    ↓
[3] Parse Output
    ├─ JSON: {"reply": "..."} → Return
    └─ JSON: {"tool": "name", "params": {...}} → Execute
    ↓
[4] Execute Tool (ToolExecutor)
    ├─ Validate params
    ├─ Run handler
    ├─ Verify result
    └─ If mutation → Read back & verify
    ↓
[5] Add Result to History (ConversationHistory)
    ├─ Compress if over token budget
    └─ Preserve most-recent tool-call/result pair
    ↓
[6] Loop (max 10 turns)
    └─ If reply → Return
    └─ If tool error → Retry or next tool
    └─ If max turns → Fallback reply
```

---

## 🧩 Core Modules

### 1. **PromptBuilder** (`brain/v2/prompt_builder.py`)

**Purpose**: Assemble a structured system prompt within a token budget.

**Components**:
- `count_tokens(text)` — Uses tiktoken if available, falls back to `len(text)//4`
- `ToolSchema` dataclass — Describes a tool's name, description, parameters
- `PromptBuilder` class — Manages priority-based section assembly

**Priority Order** (high → low):
1. **Identity** (mandatory) — "You are Alfred..."
2. **Rules** (high) — Tool format, guardrails, safety rules
3. **Profile** (high) — T4 user profile data
4. **Tools** (medium) — Available tool schemas
5. **Skills** (medium) — Top 5 T2 skills (recent/relevant)
6. **Memory** (low) — T3 episodic snippets (1-2 most relevant)

**Algorithm**:
```python
result = ""
for section in [identity, rules, profile, tools, skills, memory]:
    if fits_budget:
        result += format_section(section)
    elif can_truncate:
        result += truncate(section, available_budget)
    else:
        skip(section)  # Drop low-priority sections
```

**Output**: `AssembledPrompt` with `system` prompt text and `token_count`

**Tests**: `build-system/test_prompt_builder.py` (6/6 pass)

---

### 2. **ContextManager** (`brain/v2/context_manager.py`)

**Purpose**: Manage conversation history with token tracking and compression.

**Components**:
- `Message` dataclass — `role` (user/assistant/tool), `content`, `token_count`, `metadata`
- `ConversationHistory` class — Maintains a rolling conversation buffer

**Key Methods**:
```python
conv = ConversationHistory(token_budget=12000)
conv.add_user("What time is it?")
conv.add_assistant("I'll check the time...")
conv.add_tool_result("time", {"hour": 14, "minute": 30})
conv.compress_if_needed()  # Trim old messages if over budget
messages = conv.to_llm_messages()  # Convert to LLM format
```

**Compression Strategy**:
- Tracks total tokens across all messages
- When over budget: remove oldest messages (keeping most-recent tool-call/result pair)
- Merge consecutive same-role messages to save tokens
- Summary message appended: "…previous conversation summary…"

**Token Management**:
```
User message    → add_user()
Assistant reply → add_assistant()
Tool result     → add_tool_result()  # Metadata: {"tool": "name"}

Total tokens <= budget always
If new message would exceed → compress_if_needed()
```

**Tests**: `build-system/test_context_manager.py` (10/10 pass)

---

### 3. **LLMRouter** (`brain/llm_router.py`)

**Purpose**: Provide reliable LLM calls with multi-provider failover and adaptive timeouts.

**Providers** (in order):
1. **Groq** (primary) — Fast, free tier available, 10s timeout
2. **OpenRouter** (fallback 1) — Aggregates many models, 20s timeout
3. **Gemini** (fallback 2) — Fallback of last resort, 20s timeout

**Adaptive Timeout Strategy**:
- Each provider has a base timeout
- On timeout: reduce timeout by ×0.75 for next call
- On success: recover timeout toward base (×1.1)
- Circuit breaker: disable provider for 30s after 2 failures

**Response Object**:
```python
response = LLMResponse(
    text="...",
    provider="groq",  # Which provider was used
    fallback_used=False,
    fallback_reason=None,
    tokens_in=850,
    tokens_out=150,
    latency_ms=1240
)
```

**Usage**:
```python
router = LLMRouter(groq_key="...", openrouter_key="...", gemini_key="...")
response = await router.call(
    system_prompt="You are Alfred...",
    user_message="What time is it?",
    messages=[],
    max_tokens=1200,
    temperature=0.1
)
```

**Tests**: `build-system/test_llm_router.py` (7/7 pass)

---

### 4. **ToolExecutor** (`brain/v2/tool_executor.py`) — 🚀 IN PROGRESS (Day 4)

**Purpose**: Dispatch tools with guardrails, validation, and mutation verification.

**Key Classes**:
- `ToolResult` — Structured result: `success`, `output`, `error`, `tool_name`, `metadata`
- `Guardrails` — Per-tool rules: `allowed`, `require_approval`, `deny_patterns`, `allowed_patterns`
- `ToolExecutor` — Registry, dispatch, validation, retry logic

**Mutation Verification** (key feature):
```python
# After a mutation tool (write_file, create_calendar, send_email):
1. Execute the tool
2. If success:
   - Call corresponding read tool (VERIFY_MAP)
   - Compare result with expected outcome
   - If mismatch → retry once or alert user
3. Inject verification result into conversation
```

**Mapping**:
```python
MUTATION_TOOLS = {"calendar", "email", "write_file", "remember", "set_reminder", ...}

VERIFY_MAP = {
    "calendar": {"read_tool": "calendar", "read_params": {"action": "agenda"}},
    "email": {"read_tool": "email", "read_params": {"action": "triage"}},
    "write_file": {"read_tool": "read_file", "read_params": {...}},
    ...
}
```

**Guardrails Example**:
```python
executor.register(
    "shell",
    handle_shell,
    guardrails=Guardrails(
        allowed=True,
        require_approval=True,  # Prompt user before running
        deny_patterns=[r"rm -rf", r"del /s"],  # Never allow these
        allowed_patterns=[r"python", r"npm"]   # Only these safe
    )
)
```

**Status**: Under development (starting Day 4)

---

### 5. **Conversation Loop** (`brain/v2/conversation.py`) — 🚀 IN PROGRESS (Day 6)

**Purpose**: Orchestrate the full LLM→tool→LLM loop (the Alfred class).

**Main Class**: `Alfred`

**Responsibilities**:
1. Initialize all components (PromptBuilder, ContextManager, ToolExecutor, LLMRouter)
2. Load bootstrap files (IDENTITY.md, SOUL.md, etc.)
3. Build system prompt with memory
4. Execute the turn loop
5. Parse LLM output (reply vs. tool call)
6. Call ToolExecutor
7. Verify mutations
8. Compress history
9. Save episodes to T3

**Public API**:
```python
async def execute(task: str, context: Optional[Dict]) -> Dict:
    """
    Execute a task.
    
    Returns:
        {
            "response": "Final reply",
            "thinking": ["[Turn 1] Tool: calendar", ...],
            "tools_called": ["calendar", "email"],
            "tool_results": [{...}, ...],
            "episodes_saved": 1
        }
    """

# Singleton wrapper
def get_alfred() -> Alfred:
    """Return global Alfred instance."""
    
async def execute_task(task: str) -> Dict:
    """Convenience: call execute on singleton."""
```

**Configuration**:
```python
class Alfred:
    CHAT_MODEL = "llama-3.1-8b-instant"
    MAX_TURNS = 10
    CONVERSATION_BUDGET = 12000
    SYSTEM_PROMPT_BUDGET = 8000
```

**Status**: Partially implemented (core loop exists, wiring incomplete)

---

### 6. **Cognitive Heartbeat** (`brain/v2/heartbeat.py`) — 🚀 IN PROGRESS (Day 7)

**Purpose**: Background loop for proactive reasoning and task automation.

**Responsibilities**:
1. Every 30 minutes (configurable):
   - Check due reminders
   - Execute due cron tasks (croniter-based)
   - Run proactive reasoning LLM call
   - Collect alerts

2. Proactive reasoning prompt:
   ```
   "You are Alfred's proactive cognition. Master Sam's goals: [...].
    Check his calendar, inbox, recent study logs, and active tasks.
    Identify any gap between his current state and his goals.
    Execute corrective actions or draft nudges."
   ```

3. Store alerts for WebSocket broadcast

**Usage**:
```python
from brain.v2.heartbeat import CognitiveHeartbeat

heartbeat = CognitiveHeartbeat(
    alfred=alfred,
    db=db,
    router=router,
    memory=memory,
    interval=1800  # 30 minutes
)
heartbeat.start()  # Background thread
alerts = heartbeat.pop_alerts()  # Get pending alerts
heartbeat.stop()
```

**Status**: Under development (starting Day 7)

---

## 📊 Component Interaction Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                         Alfred (Singleton)                   │
│  - execute(task) → orchestrates all components              │
│  - _build_system_prompt() → PromptBuilder                   │
│  - _parse_llm_output() → JSON parsing                       │
│  - _maybe_generate_skill() → auto-skill generation          │
└─────────────────────────────────────────────────────────────┘
         ↓              ↓              ↓              ↓
    ┌────────┐  ┌──────────────┐  ┌────────────┐  ┌─────────┐
    │Prompt  │  │Context       │  │Tool        │  │LLM      │
    │Builder │  │Manager       │  │Executor    │  │Router   │
    └────────┘  └──────────────┘  └────────────┘  └─────────┘
         ↓            ↓                   ↓              ↓
    System   Conversation      Tool handlers    LLM providers
    Prompt   History (T1)      (registered)     Groq, OpenRouter,
             Compression       Mutation verify  Gemini
             Role alternation  Guardrails
```

---

## 🔐 Data Flow Example: "Set a reminder for tomorrow at 9am"

```
1. USER INPUT
   task = "Set a reminder for tomorrow at 9am"
   
2. PROMPT BUILDING
   PromptBuilder.assemble():
   ├─ Identity: "You are Alfred..."
   ├─ Rules: "Output ONE JSON: ..."
   ├─ Profile: "User: Master Sam, Goals: [...]"
   ├─ Tools: "set_reminder, list_reminders, ..."
   ├─ Skills: (top T2 skills)
   └─ Memory: (T3 snippet about past reminders)
   → system_prompt (2500 tokens)
   
3. CONVERSATION INIT
   ConversationHistory(budget=12000):
   → add_user("Set a reminder...")
   
4. LLM CALL
   LLMRouter.call():
   ├─ Try Groq (10s timeout)
   ├─ Fallback: OpenRouter (20s)
   └─ Fallback: Gemini
   → response = '{"tool": "set_reminder", "params": {"text": "...", "when": "..."}}'
   
5. PARSE
   _parse_llm_output(response):
   → (None, "set_reminder", {"text": "...", "when": "..."})
   
6. EXECUTE TOOL
   ToolExecutor.execute("set_reminder", params):
   ├─ Validate params
   ├─ Run handler
   ├─ success = True, output = "Reminder ID: 12345"
   
7. MUTATION VERIFY
   ToolExecutor.verify_mutation("set_reminder"):
   ├─ Call "list_reminders" to confirm
   ├─ Check if new reminder appears
   → verification success
   
8. ADD TO HISTORY
   ConversationHistory.add_tool_result("set_reminder", {...}):
   ├─ Add message: role=tool, content=output
   ├─ Track tokens
   ├─ Compress if needed
   
9. NEXT TURN
   LLM sees tool result → "Reminder set successfully"
   → Generates reply: '{"reply": "Done! I've set a reminder for tomorrow at 9am"}'
   
10. PARSE
    _parse_llm_output(reply):
    → ("Done! I've set a reminder...", None, None)
    
11. RETURN
    execute() returns:
    {
        "response": "Done! I've set a reminder for tomorrow at 9am",
        "thinking": ["[LLM provider=groq]", "[Turn 1] Tool: set_reminder", "  OK (18 chars)", "Verified: set_reminder"],
        "tools_called": ["set_reminder"],
        "tool_results": [{"tool": "set_reminder", "output": "Reminder ID: 12345", "success": True}],
        "episodes_saved": 0
    }
```

---

## 🧠 Memory Integration (T1-T5)

| Tier | Storage | Purpose | Usage in v2 |
|------|---------|---------|-------------|
| **T1** | ConversationHistory (in-memory) | Current turn context | Passed to LLM |
| **T2** | Disk (skills/) | Procedures for repeated tasks | Future: matched before LLM |
| **T3** | SQLite + FAISS | Past episodes & learnings | Snippets injected into prompt |
| **T4** | SQLite (key-value) | User profile | Injected as "Profile" section |
| **T5** | SQLite FTS5 | Full-text archive | Fallback search |

**In Prompt**:
- T4 (profile) → Identity + Profile sections
- T3 (episodes) → Memory section (top 2 snippets)
- T2 (skills) → Skills section (top 5 skills)

---

## ⚙️ Configuration Points

### Token Budgets
- `SYSTEM_PROMPT_BUDGET = 8000` — Max tokens for system prompt
- `CONVERSATION_BUDGET = 12000` — Max tokens for conversation history

### Turn Limits
- `MAX_TURNS = 10` — Max LLM→tool loop iterations per task

### LLM Settings
- `CHAT_MODEL = "llama-3.1-8b-instant"` — Default model via Groq
- `temperature = 0.1` — Low randomness (precise)
- `max_tokens = 1200` — Max output per LLM call

### Heartbeat
- `DEFAULT_INTERVAL = 1800` — 30 minutes between cognitive ticks

---

## 🔍 Debugging & Observability

### Thinking Log
Every response includes a `thinking` list:
```python
"thinking": [
    "[LLM provider=groq]",
    "[Turn 1] Tool: calendar",
    "  OK (150 chars)",
    "[Turn 2] Tool: email",
    "  X email: rate limit exceeded",
    "[Turn 3] Reply: Done! Calendar and email updated.",
    "Saved to T3 episodic memory"
]
```

### Tool Results
```python
"tool_results": [
    {"tool": "calendar", "output": "Created event: ...", "success": True},
    {"tool": "email", "output": "...", "success": False},
]
```

### Error Handling
- Non-fatal: logged in thinking, loop continues
- Fatal: MAX_TURNS reached → fallback reply
- Mutation failure: retry once, then alert user

---

## 🚀 Next Steps (Phase 1)

**Day 4**: ToolExecutor core dispatch & guardrails  
**Day 5**: Wire in tool handlers  
**Day 6**: Finalize conversation loop  
**Day 7**: Heartbeat implementation  
**Day 8**: Public API wiring  
**Day 9**: Full test suite run  
**Day 10**: Token tuning & polish  

See [docs/PHASES.md](docs/PHASES.md) for detailed checklist.
