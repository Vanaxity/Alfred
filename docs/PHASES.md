# Alfred Development Roadmap — Phase 1-5

> **Vision**: Transform Alfred from a reactive chatbot into a sovereign AGI assistant that learns, verifies, and proactively reasons.

---

## 📊 Progress Overview

| Phase | Title | Duration | Status |
|-------|-------|----------|--------|
| **1** | Sovereign Core | Weeks 1-2 | 🚀 In Progress |
| **2** | Universal Connectivity | Weeks 3-5 | ⏳ Planned |
| **3** | Self-Evolving Mind | Weeks 6-9 | ⏳ Planned |
| **4** | JARVIS Multimodal | Weeks 10-12 | ⏳ Planned |
| **5** | AGI Trajectory | Ongoing | 🎯 Vision |

**Manifesto Coverage**: 43/58 gaps closed (74%)

---

## 🎯 Phase 1: Sovereign Core (Weeks 1-2)

**Goal**: Replace reactive behavior with memory-driven, self-verifying, proactive execution.

### Why Phase 1 Matters
Despite having 30+ tools and a 5-tier memory system, Alfred still:
- Forgets its own skills (T2 never matched)
- Doesn't verify mutations (claims success without reading back)
- Doesn't reason proactively (heartbeat is just a fetch loop)
- Uses brittle regex (false positives, misrouted intents)

These 4 gaps alone prevent true sovereignty. Phase 1 closes them.

### Completed (Days 1-3) ✅

#### Day 1: Project Scaffolding
- ✅ Create brain/v2/ folder with clean architecture
- ✅ Copy current alfred.py as reference (brain/v2/alfred_v2.py)
- ✅ Add stub get_alfred() to preserve import chain
- ✅ Run baseline test suite
  - **Baseline**: phase1_d1d2_test.py: 10/13 PASS (3 timeouts)
  - **Baseline**: phase1_cockpit_test.py: 13/20 PASS (7 failures)

#### Day 2: PromptBuilder Core ✅
- ✅ Implement `count_tokens()` with tiktoken fallback
- ✅ Build `PromptBuilder.assemble()` with priority order:
  - Identity (mandatory)
  - Rules (high)
  - Profile (high) — T4 user profile
  - Tools (medium) — Available tool schemas
  - Skills (medium) — Top 5 T2 skills
  - Memory (low) — T3 episodic snippets
- ✅ Add ToolSchema dataclass and to_prompt_block() format
- ✅ Write token budget trimming tests
- ✅ Export from brain/v2/__init__.py
- **Tests**: 6/6 PASS ✅

#### Day 3: ContextManager (Conversation History) ✅
- ✅ Define Message dataclass (role, content, token_count, metadata)
- ✅ Implement ConversationHistory with:
  - `add_user()`, `add_assistant()`, `add_tool_result()`
  - `to_llm_messages()` for LLM formatting
  - `compress_if_needed()` preserving most-recent tool-call/result
  - Role alternation (no consecutive same-role messages)
- ✅ Write compression tests (budget exceeded → trim old messages)
- ✅ Export from brain/v2/__init__.py
- ✅ Add shared `count_tokens()` import (removed duplicate)
- **Tests**: 10/10 PASS ✅

**Bonus**: LLM Router with adaptive timeouts
- ✅ Implement multi-provider failover (Groq → OpenRouter → Gemini)
- ✅ Adaptive timeouts (shrink ×0.75 on timeout, recover on success)
- ✅ Circuit breaker (disable for 30s after 2 failures)
- ✅ Provider stats observability
- **Tests**: 7/7 PASS ✅

---

### In Progress (Days 4-10) 🚀

#### Day 4/5: ToolExecutor — Guardrails, Self-Correcting Retry, Verification ✅

- [x] DONE (2026-08-17) — merged Day 4 and Day 5 into one unit.

**What was actually found.** `brain/v2/tool_executor.py` was *not* a stub. It already
contained a complete 976-line port of `alfred.py`'s dispatcher: all 23 tools
implemented as `async def handle_*(params, ctx) -> ToolResult`, registered via
`create_tool_executor()`, and already wired into `conversation.py`. Same pattern as
Days 2-3 (module pre-existed but was unfinished). So the Day 5 "write thin async
wrappers" work was already done, and the real work was closing the gaps between
what existed and the requirements.

**Gaps closed:**
- [x] Guardrails were dead config — `Guardrails.require_approval`, `deny_patterns`,
      and `allowed_patterns` existed on the dataclass but `execute()` only checked
      `.allowed`. Now fully enforced via `check_guardrails()`.
- [x] Populated `TOOL_GUARDRAILS` — `shell`, `run_code`, `open_app` require explicit
      approval (granted via `context["approved_tools"]`), plus `_DESTRUCTIVE_PATTERNS`
      as a deny-list backstop that holds *even when approval is granted*.
- [x] Retry was a pointless same-params repeat. Replaced with LLM-driven param
      correction: the tool's error is fed back to the router, which proposes corrected
      params, up to `MAX_TOOL_ATTEMPTS` (3). Every attempt is recorded in
      `metadata["attempts"]` so a final failure reports what was actually tried.
      Corrected params are re-checked against guardrails so the LLM cannot talk its
      way past a deny pattern across a retry.
- [x] `VERIFY_MAP` covered only 4 of 8 mutation tools. Added `delete_reminder`,
      `write_file`, `memory_save`; `run_code` is an explicit `NO_READBACK` exception
      (re-running it to verify would repeat its side effects).
- [x] Added a `carry` mechanism to `VERIFY_MAP` so read-back targets what was actually
      written (`write_file` → `read_file` on the same path). This also fixed a silent
      bug: `remember`'s verification called `memory_search` with no `query`, which that
      handler rejects outright — so that verification had never once worked.
- [x] Mutation detection was coarse — `calendar`/`email` counted as mutations even for
      read actions (`agenda`, `triage`). Now action-aware via `is_mutation()`.
- [x] `_run_once()` guards against a handler returning a non-`ToolResult`.
- [x] Exported the ToolExecutor API from `brain/v2/__init__.py` (was unreachable except
      by direct submodule import).
- [x] `build-system/test_tool_executor.py` — **33/33 passing**, verified non-vacuous
      against the pre-fix implementations.
- [x] Added `__main__` demo block.

**Suite results after this work:**

| Suite | Before | After |
|---|---|---|
| test_prompt_builder | 6/6 | 6/6 |
| test_context_manager | 10/10 | 10/10 |
| test_llm_router | 7/7 | 7/7 |
| test_tool_executor | *(did not exist)* | **33/33** |
| phase1_d1d2 | 10/13 | **10/11 (91%)** |
| phase1_cockpit | 13/20 + 1 ERROR | **19/20 (84%, Grade B)** |

Remaining failures are both pre-existing data gaps, not code defects: d1d2 B11 needs a
populated neural-memory index (`neural_memories.json` is absent from both the old and
new repo), and cockpit T07 needs Master Sam's own email address in the T4 profile —
Alfred currently asks for it rather than guessing, which is arguably correct.

⚠️ `shell`, `run_code`, and `open_app` now require approval via
`context["approved_tools"]`, and nothing grants it yet — so those three are effectively
unusable until Day 6 wires the approval prompt. Neither test suite exercises them, so
this is invisible to the suites but would be visible to a user.

**Security fixes** (found while reading the code, scoped to the active v2 path only):
- [x] `glob` had *zero* path-safety gating while its sibling file tools all had it —
      `C:/Windows/**/*` was enumerable. Now gated, with results filtered too (relative
      patterns and symlinks can escape even a safe-looking pattern).
- [x] `open_app` shell-injected: `subprocess.Popen([exe], shell=True)` with `exe`
      falling back to the raw user string, so `"notepad & del *.*"` reached `cmd.exe`
      verbatim. Now resolves via `shutil.which()` with `shell=False` and refuses
      metacharacters.
- [x] `_is_safe_path` used `str.startswith()` with no separator boundary, so
      `C:\Users\<u>_evil\secret.txt` passed as inside the home directory. Confirmed
      exploitable against the old code, now uses `Path.is_relative_to()`.

**Bonus fix — the entire LLM provider chain was dead:**
- [x] Groq (priority 1, *primary*): `llama-3.1-8b-instant` → `model_not_found`.
      Replaced with `openai/gpt-oss-120b` + `reasoning_effort="low"`. The flag is
      load-bearing: without it the model burns its whole budget on reasoning and
      returns empty content, or attempts native tool-calling and 400s.
      Benchmarked 0.65s avg, 10/10 correct on Alfred's JSON protocol.
- [x] Gemini (priority 3): `gemini-2.0-flash` absent from the model list.
      Replaced with `gemini-2.5-flash`.
- [x] OpenRouter (priority 2): alive but leaked `<think>` reasoning into `content`.
      Added `strip_reasoning()` applied to all provider return paths.
- [x] Latent bug: `_probe_provider` used `max_tokens=1`, which always yields empty
      content on a reasoning model — so once the Groq breaker opened it could never
      recover. Probe now tolerates an empty completion as proof of reachability.

**Deferred:** `handle_calendar` dropped the duplicate-time-slot ambiguity detection
that `alfred.py:1748-1760` has. UX regression, not a correctness bug — revisit in Day 6.

**Deliverables**: `brain/v2/tool_executor.py`, `brain/llm_router.py`,
`brain/v2/__init__.py`, `brain/v2/conversation.py`, `build-system/test_tool_executor.py`

---

#### Day 6: Conversation Loop — Main LLM→Tool→LLM

**Objectives**:
- [ ] Finish Alfred class with __init__:
  - [ ] PromptBuilder instance
  - [ ] ConversationHistory instance
  - [ ] ToolExecutor via create_tool_executor()
  - [ ] LLMRouter instance
  - [ ] Bootstrap files (IDENTITY.md, SOUL.md, etc.)
- [ ] Implement _build_system_prompt():
  - [ ] Priority-ordered assembly via PromptBuilder
  - [ ] Identity + Rules (bootstrap) + Profile (T4) + Tools + Skills (T2) + Memory (T3)
- [ ] Implement execute() loop:
  - [ ] Build prompt → LLM call → parse output
  - [ ] If reply → return Response
  - [ ] If tool → executor.execute() → add result → compress
  - [ ] Mutation verification (if success & tool in MUTATION_TOOLS)
  - [ ] Max-turns limit (10)
  - [ ] Fallback reply if no tools used
- [ ] Implement Response dataclass
- [ ] Add thin wrapper execute_task() → singleton Alfred
- [ ] Write integration test (dummy LLM that returns reply)

**Acceptance Criteria**:
- Full turn loop works (LLM→tool→LLM→reply)
- Compression preserves tool results
- Max turns enforced
- Response includes thinking, tools_called, tool_results

**Deliverables**:
- `brain/v2/conversation.py` (finalized Alfred class)
- `build-system/test_conversation.py` (integration test)

---

#### Day 7: Heartbeat — Cognitive Proactive Loop

**Objectives**:
- [ ] Implement CognitiveHeartbeat class with:
  - [ ] `tick()` → one full cycle
  - [ ] `start()` → daemon thread, configurable interval (default 1800s)
  - [ ] `stop()` → graceful shutdown
- [ ] Tick workflow:
  - [ ] Check due reminders (existing path)
  - [ ] Execute due cron tasks (croniter-based, from local_db)
  - [ ] Run proactive reasoning LLM call:
    ```
    "You are Alfred's proactive cognition.
     Master Sam's goals: [T4 goals].
     Check calendar, inbox, recent logs, active tasks.
     Identify gaps. Execute corrective actions or draft nudges."
    ```
  - [ ] Store alerts in _pending_alerts
- [ ] Implement pop_alerts() for WebSocket broadcast
- [ ] Wire into Alfred.__init__() and start()
- [ ] Optional Alfred.stop() for clean shutdown

**Acceptance Criteria**:
- Heartbeat ticks every 30 minutes
- Reminders checked and fired
- Cron tasks executed
- Proactive reasoning prompt runs (even if response is empty for now)
- Alerts stored and poppable

**Deliverables**:
- `brain/v2/heartbeat.py` (CognitiveHeartbeat)
- Wiring in `brain/v2/conversation.py` (start in __init__)

---

#### Day 8: Wire Public API & Test Server

**Objectives**:
- [ ] Ensure brain/__init__.py imports from .v2:
  ```python
  from .v2 import get_alfred, execute_task, Alfred
  ```
- [ ] Verify brain_api/server.py still works:
  - [ ] GET /api/health
  - [ ] POST /api/execute
  - [ ] WebSocket /ws
- [ ] Run server in dev mode
- [ ] Fix any import errors
- [ ] Test health endpoint

**Acceptance Criteria**:
- `python -m brain_api.server` starts without errors
- `curl http://localhost:8001/api/health` returns 200
- No regressions in existing endpoints

**Deliverables**:
- Updated `brain/__init__.py`
- Updated `brain_api/server.py` (if needed)

---

#### Day 9: Run Full Test Suites & Fix Failures

**Objectives**:
- [ ] Execute phase1_d1d2_test.py (14 tests)
- [ ] Execute phase1_cockpit_test.py (20 questions)
- [ ] For each failure:
  - [ ] Add logging/print to locate issue
  - [ ] Debug (missing tool? token budget? parsing error?)
  - [ ] Fix in appropriate module
  - [ ] Re-run suite
- [ ] Target: 100% pass rate

**Known Issue Areas**:
- T4 profile injection (fixed: check _build_system_prompt)
- T3 episodic memory injection (fixed: add to Memory section)
- Tool timeouts (handled: LLMRouter with adaptive timeouts)
- Goal inference (currently disabled: will re-enable later)

**Acceptance Criteria**:
- phase1_d1d2_test.py: 14/14 PASS
- phase1_cockpit_test.py: 20/20 PASS
- No known regressions

**Deliverables**:
- Updated phase1 test files (if needed)
- Session log: `sessions/2026-08-17-day9-test-suite.md`

---

#### Day 10: Token Budget Tuning & Final Polish

**Objectives**:
- [ ] Gather representative dialogues from test suite
- [ ] Measure token usage (use count_tokens())
- [ ] Set CONVERSATION_BUDGET and SYSTEM_PROMPT_BUDGET to 95th-percentile + margin
  - Suggested starting: CONVERSATION_BUDGET = 12000, SYSTEM_PROMPT_BUDGET = 8000
- [ ] If MAX_TURNS reached often → consider increasing to 12-15
- [ ] Ensure logging uses standard library (optional)
- [ ] Remove stray print statements
- [ ] Verify __all__ in __init__.py exports only public API
- [ ] Create README snippet in brain/v2/

**Acceptance Criteria**:
- Budgets handle 95th-percentile of real dialogues
- No conversations get truncated mid-important-section
- Code is clean (no debug prints, minimal logging)
- __all__ is complete and correct

**Deliverables**:
- Updated CONVERSATION_BUDGET and SYSTEM_PROMPT_BUDGET constants
- Final `brain/v2/README.md` (module breakdown)

---

### Final Verification (Day 10 Continued)

- [ ] Run all test suites one last time
- [ ] Aim for 100% green
- [ ] Update docs/PHASES.md "Last Updated" date
- [ ] Create comprehensive docs in docs/ (API.md, TOOLS.md, MEMORY.md)
- [ ] Commit with message:
  ```
  feat: refactor to Hermes-inspired v2 architecture – Phase 1 complete
  
  - Implemented PromptBuilder with token budgeting
  - Implemented ContextManager with compression
  - Implemented ToolExecutor with guardrails
  - Wired in 15+ tool handlers
  - Conversation loop with max-turns and mutation verification
  - Cognitive heartbeat with proactive reasoning
  - All Phase 1 tests passing (14/14 d1d2, 20/20 cockpit)
  - Token budgets tuned to 95th-percentile
  ```

**Phase 1 Status**: ✅ COMPLETE (Target: 2026-09-01)

---

## 🔌 Phase 2: Universal Connectivity (Weeks 3-5)

**Goal**: Alfred gains the ability to interface with ANY digital service through an open protocol.

### Key Initiatives

#### MCP Client Implementation
- [ ] Implement `brain/mcp_client.py`
- [ ] Spawn MCP servers (stdio-based JSON-RPC)
- [ ] Discover tools via `/list` endpoint
- [ ] Dynamically register tools into ToolExecutor
- [ ] No hardcoding — pure protocol

#### Tool Expansion
With MCP, immediately unlock:
- [ ] Filesystem (read/write in safe dirs)
- [ ] Google Drive, Sheets, Docs
- [ ] Slack, Discord, Telegram, WhatsApp
- [ ] GitHub, GitLab
- [ ] Databases (PostgreSQL, SQLite)
- [ ] Home Assistant (smart home)
- [ ] Community MCP servers (unlimited)

#### Duplicate Codebase Consolidation
- [ ] Merge `project-alfred/brain_api/` and `alfred-cockpit/server/brain_api/`
- [ ] Resolve hardcoded API keys
- [ ] Single source of truth

**Acceptance Criteria**:
- Filesystem MCP server connected
- Slack MCP server connected
- 3+ new tools working via MCP
- All Phase 1 tests still pass

**Impact**: Alfred surpasses OpenClaw's 13,000 community skills (quality > quantity).

---

## 🧠 Phase 3: Self-Evolving Mind (Weeks 6-9)

**Goal**: Alfred writes its own tools, patches its own skills, and optimizes its own performance.

### Key Initiatives

#### Skill Validation & Versioning
- [ ] Dry-run tests for T2 skills (sandbox)
- [ ] SemVer versioning for skills
- [ ] Drafts folder for failed skills

#### Tool Forge (Skill → Python Code)
- [ ] On skill used 3+ times: auto-convert markdown to Python
- [ ] Validate in subprocess sandbox
- [ ] Register as new permanent tool
- **Impact**: Alfred expands its own capabilities autonomously

#### Self-Audit Loop
- [ ] Weekly cron: feed Alfred its own logs
- [ ] Prompt: "Identify inefficiencies and propose one optimization"
- [ ] Changes applied (in sandbox mode, user approves)

#### Proactive Goal Decomposition
- [ ] Break long-term goals into near-term sub-goals
- [ ] Track progress autonomously
- [ ] Adjust daily plans

**Acceptance Criteria**:
- 3 skills auto-converted to tools
- Self-audit loop runs weekly
- Goal decomposition works for 1 complex goal
- All Phase 1-2 tests still pass

**Impact**: Alfred is no longer static; it's a developer itself.

---

## 🎬 Phase 4: JARVIS Multimodal Layer (Weeks 10-12)

**Goal**: Alfred sees, hears, and controls the physical environment.

### Key Initiatives

#### Full Voice Autonomy
- [ ] Wake word detection (openWakeWord custom "Alfred" model)
- [ ] Continuous conversation (listen for follow-ups without re-waking)
- [ ] Local Faster-Whisper STT
- [ ] Edge-TTS voice synthesis

#### Vision Integration
- [ ] Face recognition (face_recognition lib)
- [ ] Presence detection (known person in room?)
- [ ] Screen OCR (read text from screen)
- [ ] Gesture control (MediaPipe hand tracking)

#### Personalization
- [ ] SOUL.md personality injection
- [ ] Voice variation by mood/mode
- [ ] Context-aware responses

**Acceptance Criteria**:
- Voice wake word works ("Hey Alfred")
- Continuous conversation flows
- Face recognition identifies Master Sam
- Gesture control works (5 gestures minimum)
- All Phase 1-3 tests still pass

**Impact**: Alfred becomes a truly embodied presence (JARVIS-like).

---

## 🌟 Phase 5: AGI Trajectory (Ongoing)

**Goal**: Alfred approaches AGI-grade capabilities through continuous self-improvement.

### Key Initiatives

#### Self-Model (Tier 0)
- [ ] Alfred knows its own architecture
- [ ] Can explain its own components
- [ ] Self-awareness layer

#### Architectural Proposal System
- [ ] Alfred proposes changes to its own design
- [ ] User reviews and approves
- [ ] Changes deployed safely

#### Sandboxed Self-Coding
- [ ] Docker container for self-modification
- [ ] Alfred writes and tests its own code
- [ ] Safe execution environment
- [ ] Invariant checking (doesn't break existing functionality)

#### Cross-Session Goal Persistence
- [ ] Goals survive restarts
- [ ] Long-term progress tracking
- [ ] Milestone achievements

#### API Authentication & Security
- [ ] OAuth2 for remote access
- [ ] Encryption for sensitive data
- [ ] Audit logs for all actions

**Acceptance Criteria**:
- Self-coding loop works (write → test → deploy)
- Long-term goal persists across sessions
- Alfred can propose 1 architectural change
- All Phase 1-4 tests still pass

**Impact**: Alfred achieves true autonomous self-improvement (AGI trajectory).

---

## 🐛 Known Issues & Bugs

### CRITICAL (Blocks Phase 1)
- [ ] Hardcoded API keys in brain_api/server.py:34-41 (move to .env)
- [ ] Calculator uses bare `eval()` (RCE in untrusted environments)

### HIGH PRIORITY (Phase 2)
- [ ] T2 skills generated but never matched (dead code)
- [ ] SQL injection via f-string table names (local_db.py:93,99,112)
- [ ] _get_conn() race condition (no lock)
- [ ] Orchestrator shell injection (interpolation into CMD)
- [ ] No graceful shutdown for FAISS/SQLite resources
- [ ] Bare except swallowing CancelledError (broadcast_to_clients)

### MEDIUM (Polish)
- [ ] SkillManager not a singleton (reloads from disk each call)
- [ ] BM25 index not updated on new T3 episodes
- [ ] T1 expiration never triggered (memory leak)
- [ ] WebSocket no ping/pong keepalive (stale clients)
- [ ] Message truncation silent (no indicator)
- [ ] T4 profile overwrites without warning

### LOW (Nice-to-Have)
- [ ] Casual greeting matching misses "yo", "heya", "sup"
- [ ] Hardcoded Windows paths for bootstrap
- [ ] Inconsistent env var naming (OBSIDIAN_VAULT_PATH vs OBSIDIAN_VAULT)
- [ ] 3 separate Python servers (no unified config)

---

## 📊 Metrics to Track

### Test Coverage
- **Unit tests**: Aim for 80%+ coverage per module
- **Integration tests**: phase1_d1d2 (14 tests), phase1_cockpit (20 tests)
- **E2E tests**: Manual verification of 3-5 complex scenarios

### Performance
- **LLM latency**: Track Groq, OpenRouter, Gemini response times
- **Token usage**: Monitor budget utilization (target: <80%)
- **Tool execution time**: Log per-tool latency (warn if >5s)

### Quality
- **Error rate**: Track tool failures, LLM parse errors
- **Retry rate**: How often do mutation verifications fail?
- **User satisfaction**: Feedback on accuracy and helpfulness

---

## 🎯 Success Criteria (Phase 1 Complete)

- ✅ All 14 d1d2 tests pass
- ✅ All 20 cockpit tests pass
- ✅ No known regressions
- ✅ Token budgets tuned to real usage
- ✅ Documentation complete (README, ARCHITECTURE, CONTRIBUTING)
- ✅ Codebase clean (no debug prints, proper error handling)
- ✅ Ready for Phase 2 (MCP client implementation)

---

## 📅 Timeline

```
Week 1 (Aug 17-23)
  ├─ Day 1-3: PromptBuilder, ContextManager, LLMRouter ✅
  ├─ Day 4-5: ToolExecutor + handlers (THIS WEEK)
  └─ Day 6: Conversation loop

Week 2 (Aug 24-30)
  ├─ Day 7: Heartbeat
  ├─ Day 8: API wiring
  ├─ Day 9: Test suite runs & fixes
  └─ Day 10: Token tuning & polish

Phase 1 COMPLETE by Sept 1, 2026
Phase 2 starts Sept 2, 2026
```

---

**Last Updated**: 2026-08-17  
**Current Phase**: 1 (Sovereign Core) — Days 4-10 in progress  
**Next Milestone**: Phase 1 complete by 2026-09-01
