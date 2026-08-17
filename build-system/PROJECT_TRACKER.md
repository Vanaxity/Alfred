# Alfred Project Tracker

Last Updated: 2026-08-14

Current Phase: Phase 1 (Sovereign Core) — In Progress

## Manifesto Coverage: 43/58 gaps closed (74%)

## Done Recently

- [x] **NEW** Day 3: ContextManager/ConversationHistory (2026-08-14) — module pre-existed and was wired into `conversation.py`, so per Master Sam's decision we kept the richer implementation (same-role merge, tool→user LLM conversion for provider compat, summary compression) and added the missing spec surface: `token_usage` property, `_maybe_compress()` hook (wired into all add paths), `Message`/`ConversationHistory` exports in `__init__.py`, `metadata["tool"]` key in `add_tool_result`, `__main__` demo, shared `count_tokens` import (removed duplicate `_count_tokens`). TDD: `build-system/test_context_manager.py` 10/10 (RED→GREEN). Compression preserves most-recent tool-call+result pair verified. `test_llm_router` 7/7, `test_prompt_builder` 6/6, `import brain_api.server` clean, ruff clean, py_compile clean. Session log: `sessions/2026-08-14-day3-context-manager.md`.
- [x] **NEW** Day 2: PromptBuilder core (2026-08-14) — module pre-existed (wired into conversation.py) but was unfinished: no `__init__.py` exports, no demo, no tests. Closed gaps via TDD (`build-system/test_prompt_builder.py` 6/6 pass, RED→GREEN). Spec deviations fixed: `to_prompt_block()` → exact spec format, truncation marker → `…[truncated]`, `count_tokens` fallback → `len(text)//4` (empty→0), `__main__` demo added, unused `field` import removed. Day 2 exports added to `brain/v2/__init__.py` imports + `__all__`. Router tests 7/7, `import brain_api.server` clean, ruff clean. Session log: `sessions/2026-08-14-day2-prompt-builder.md`.
- [x] **NEW** Fail-fast LLM router (2026-08-10) — adaptive per-provider timeouts (groq 10s / openrouter & gemini 20s, shrink ×0.75 on timeout, recover toward base on success), circuit breaker tightened to 2 failures / 30s recovery, lazy 3s preflight probe on HALF_OPEN, `get_stats()` observability, provider+fallback note appended to `thinking[]`. Files: `brain/llm_router.py`, `brain/v2/conversation.py`. Tests: `build-system/test_llm_router.py` (7/7 pass). Bonus fix: rate-limit failover now correctly sets `fallback_used=True` (previously skipped the flag, so fallback was reported as primary).
- [x] **NEW** Day 1 baseline captured (2026-08-10) — d1d2 10/13, cockpit 13/20 + 1 timeout error.

## Phase 1: Sovereign Core (Rebuilt Hermes‑Inspired Architecture) — In Progress

### Week 1
Day 1: Project scaffolding & baseline
- [x] DONE (2026-08-10) Create brain/v2/ folder and __init__.py — dir exists; __init__.py already exports Alfred/get_alfred/execute_task (surpassed stub)
- [x] DONE (2026-08-10) Copy current alfred.py -> brain/v2/alfred_v2.py (reference) — reference copy created (117,669 bytes)
- [x] DONE (2026-08-10) Add stub get_alfred() to keep import chain — superseded by real singleton in brain/v2/__init__.py:21-26
- [x] DONE (2026-08-10) Run baseline test suite to record failures — see BASELINE note below

**BASELINE (2026-08-10, v3.0.0):**
- phase1_d1d2_test.py: 10/13 PASS — FAIL B03/B04/B05 (T4/T3 command timeouts)
- phase1_cockpit_test.py: 13/20 PASS, 1 ERROR — FAIL T01/T04/T05/T06/T07/T14 (goals, calendar, email auth, invalid-time), ERROR T13 (timeout)

Day 2: PromptBuilder core
- [x] DONE (2026-08-14) Implement count_tokens() (tiktoken with fallback)
- [x] DONE (2026-08-14) Build PromptBuilder.assemble() with priority order Identity > Rules > Profile > Tools > Skills > Memory
- [x] DONE (2026-08-14) Add ToolSchema dataclass and to_prompt_block()
- [x] DONE (2026-08-14) Write unit-test for token budget trimming
- [x] DONE (2026-08-14) Export PromptBuilder from __init__.py

Day 3: ContextManager (history & compression)
- [x] DONE (2026-08-14) Define Message dataclass (role, content, token_count, metadata)
- [x] DONE (2026-08-14) Implement ConversationHistory with add_user, add_assistant, add_tool_result, to_llm_messages(), compress_if_needed() preserving most recent tool-call/result pair
- [x] DONE (2026-08-14) Enforce role alternation (no two consecutive same-role non-summary messages)
- [x] DONE (2026-08-14) Write test that exceeds budget and verifies compression
- [x] DONE (2026-08-14) Export ConversationHistory from __init__.py

Day 4: ToolExecutor – core dispatch & guardrails
- Sketch ToolResult dataclass (success, output, error, tool_name, metadata)
- Build ToolExecutor with register/register_legacy
- Implement guardrail check (allowed/deny, require_approval)
- Implement validation (custom validator or ToolResult.success)
- Add one-retry on mutation-tool failure
- Add verify_mutation() using VERIFY_MAP
- Export ToolExecutor and create_tool_executor() from __init__.py

Day 5: ToolExecutor – wire in existing tool handlers
- For each tool needed by test suite (time, calculator, calendar, email, web_search, shell, read_file, write_file, memory_save, memory_search, set_reminder, list_reminders, delete_reminder):
  * Add thin async wrapper calling existing implementation
  * Register via create_tool_executor()
- Run sanity checks for time and calculator

### Week 2
Day 6: Conversation loop – main LLM→tool→LLM
- Finish Alfred class: __init__ with PromptBuilder, ConversationHistory, ToolExecutor (via create_tool_executor()), LLMRouter
- _build_system_prompt() pulls identity, profile (memory), rules (bootstrap), tools (executor.schemas), skills/memory snippets (memory) and calls prompt_builder.assemble()
- Turn loop: build prompt → LLM call → parse output → if reply break; if tool → executor.execute → (if mutation) verify → add result to history → history.compress_if_needed()
- Implement Response dataclass and public execute() method
- Add thin wrapper execute_task() returning singleton and calling await alfred.execute()
- Run sanity test with dummy LLM that always returns a reply

Day 7: Heartbeat – cognitive loop
- Finish CognitiveHeartbeat class: tick() does reminder check, cron-task check, proactive reasoning
- start() spawns daemon thread running tick every interval (default 1800s)
- stop() stops thread
- Alerts stored in self._pending_alerts, exposed via pending_alerts/pop_alerts() for WebSocket
- In Alfred.__init__() instantiate CognitiveHeartbeat and call start()
- Optional Alfred.stop() for clean shutdown

Day 8: Wire the public API & test the server
- Ensure brain/__init__.py imports from .v2: from .v2 import get_alfred, execute_task, Alfred
- Verify brain_api/server.py still imports correctly
- Run server in dev mode and hit a simple endpoint (e.g., /api/health) to confirm v2 usage
- Fix any import errors

Day 9: Run the official test suites & fix failures
- Execute phase1_d1d2_test.py (14 tests) and phase1_cockpit_test.py (20 questions)
- For each failure, add logging/print to locate issue (missing tool, token budget low, parsing)
- Fix issues iteratively until both suites pass (100%)
- Keep note of changes for commit

Day 10: Token‑budget tuning & final polish
- Gather representative dialogues from test suite/manual interaction
- Measure token usage using shared count_tokens() and set CONVERSATION_BUDGET / SYSTEM_PROMPT_BUDGET to 95th‑percentile + small margin (start with 12000/8000)
- If max turns reached often, consider increasing MAX_TURNS or loosening budget
- Ensure logging uses standard library in each major class (optional)
- Clean up: remove stray print statements, verify __all__ in __init__.py exports only public API

### Final Verification (Day 10 continued)
- Run test suites one last time; aim for 100% green
- Update PROJECT_TRACKER.md "Last Updated" date
- Add short README snippet in brain/v2/ explaining each module
- Commit with message: "feat: refactor to Hermes‑inspired v2 architecture – Phase 1 complete"

## Phase 2: Universal Connectivity (6 gaps)

- [ ] 17. Implement MCP client module (brain/mcp_client.py)
- [ ] 18. Connect filesystem MCP server
- [ ] 19. Connect Slack/Discord MCP servers
- [ ] 20. Connect GitHub MCP server
- [ ] 21. Dynamic tool registration from MCP servers
- [ ] 22. Consolidate duplicate codebases — project-alfred/brain_api/ and alfred-cockpit/server/brain_api/ diverging with hardcoded keys in one copy

## Phase 3: Self-Evolving Mind (9 gaps)

- [ ] 23. Skill validation sandbox (dry-run skills before saving)
- [ ] 24. Skill versioning (SemVer for T2 skills)
- [ ] 25. Tool Forge: convert skills to Python code
- [ ] 26. Self-audit cron job (weekly review of own logs)
- [ ] 27. Proactive goal decomposition (break long-term goals into sub-tasks)
- [ ] 28. Token budget tracking across iterations
- [ ] 29. Wire _handle_error_smart() smart recovery into _execute_with_retry() — currently dead code
- [ ] 30. Fix improve_skill() — doesn't update in-memory cache, only writes to disk (skill_manager.py:225-246)
- [ ] 31. Integrate RetryHandler from recovery.py — currently dead code, alfred.py has its own inline retry

## Phase 4: JARVIS Multimodal Layer (5 gaps)

- [ ] 32. Wake word detection (openWakeWord)
- [ ] 33. Continuous conversation mode
- [ ] 34. Voice personality variation by SOUL.md mode
- [ ] 35. Face recognition + presence detection
- [ ] 36. Gesture control (MediaPipe hand tracking)

## Phase 5: AGI Trajectory (5 gaps)

- [ ] 37. Self-Model (Tier 0) — Alfred knows its own architecture
- [ ] 38. Architectural proposal system
- [ ] 39. Sandboxed self-coding (Docker container for self-modification)
- [ ] 40. Cross-session goal persistence
- [ ] 41. API authentication

## Architecture & Infrastructure Gaps (10 items)

- [ ] 42. Fix SQL injection via f-string table name in local_db.py:93,99,112
- [ ] 43. Fix _get_conn() race condition — not protected by lock (local_db.py:30-37)
- [ ] 44. Add WebSocket reconnection in cockpit frontend — currently lost permanently on blip (page.tsx:97-123)
- [ ] 45. Fix ngrok pipe deadlock — stdout PIPE never read (brain_api/server.py:149-157)
- [ ] 46. Fix blocking time.sleep() in async lifespan — delays server startup (brain_api/server.py:160-177)
- [ ] 47. Fix broadcast_to_clients() bare except — swallows CancelledError (brain_api/server.py:241-242)
- [ ] 48. Add graceful shutdown for FAISS/SQLite resources (all brain modules)
- [ ] 49. Replace print() with structured logging across entire codebase
- [ ] 50. Fix stale closure over currentSessionId in page.tsx:handleCommand
- [ ] 51. Fix switchSession() referencing stale messages state (page.tsx:229-237)
- [x] **NEW** Cron scheduler — `get_due_scheduled_tasks()` in local_db.py using croniter, wired into Alfred heartbeat every 30s, executes due cron tasks through full 4-phase loop, pushes results as alerts.
- [x] **NEW** `start_alfred.ps1` rewritten — corrected paths, ngrok token via `NGROK_AUTH_TOKEN` env var, Vercel deploy step, cleaner output.

## Known Bugs

- [ ] Hardcoded API keys for Groq/Google/OpenRouter committed in project-alfred/brain_api/server.py:34-41
- [ ] Calculator uses bare `eval()` — RCE vulnerability
- [ ] T2 skills generated but never matched — dead code
- [x] CRITICAL: T3 episodic memory never injected into planner — broken feature
- [x] CRITICAL: Goal inference permanently disabled
- [x] CRITICAL: No API authentication on brain API
- [x] CRITICAL: No message persistence to server
- [x] CRITICAL: No response streaming in cockpit
- [x] CRITICAL: `_classify_reply` regex fragile — false positives
- [ ] HIGH: T2 skills generated but never matched — find_skill() and t2_find_skill() never called (dead code)
- [ ] HIGH: _handle_error_smart() defined but never invoked in retry loop
- [ ] HIGH: _classify_reply regex false positives — "can't", "unable" match legitimate status messages
- [x] HIGH: get_context_for_llm() called without query — T3 episodic memory never injected via this method
- [ ] HIGH: Goal inference hard-disabled (enabled=False) and sync HTTP blocks event loop
- [ ] HIGH: _intent_based_plan() maps "file" intent to web_search instead of file tools
- [ ] HIGH: IntentClassifier "read " keyword matches "already read", "thread", "spreadsheet" — massive false positives
- [ ] HIGH: IntentClassifier "today" and "open" keywords too broad — wrong routing on common phrases
- [ ] HIGH: LLM classification path algorithmically unreachable — keyword confidence always >= 0.8 threshold
- [ ] HIGH: Safe directories hardcoded to ruchi user — file tools unusable cross-platform
- [ ] HIGH: reasoning=True passed to Groq models that don't support it — API error (alfred.py:1144)
- [ ] HIGH: No verification loop after mutation tools — false success reported
- [ ] HIGH: Missing import re in skill_manager.py — NameError on generate_skill() with special characters
- [ ] HIGH: Heartbeat has no cognitive processing — just fetches calendar/email
- [ ] HIGH: WebSocket in cockpit never reconnects — permanent disconnect after server restart
- [ ] HIGH: Autonomous loop Hermes output prefers stderr over stdout — research results silently lost
- [ ] HIGH: start_ngrok() pipe deadlock — stdout never read, OS buffer fills up

## MEDIUM: Duplicate codebases — project-alfred/brain_api/ and alfred-cockpit/server/brain_api/ drifing apart
- [ ] MEDIUM: SkillManager not a singleton — creates new instance on every call, reloads all skills from disk
- [ ] MEDIUM: BM25 index not updated when new T3 episodes saved — search misses recent memories
- [ ] MEDIUM: FTS5 sanitization strips valid `*`, `+`, `-` operators — breaks advanced search
- [ ] MEDIUM: BM25 score normalization capped at 1.0 — arbitrary /10.0 reduces hybrid search quality
- [ ] MEDIUM: Docstring says hybrid weights Vector×0.5 / Keyword×0.3 / Recency×0.2 but code uses 0.4/0.3/0.3
- [ ] MEDIUM: T1 expiration never triggered automatically — memory leak over long sessions
- [ ] MEDIUM: Unused `lru_cache` import in five_tier.py
- [ ] MEDIUM: T4 profile parser creates phantom "Sam's User Profile" section from `#` header
- [ ] MEDIUM: _suggest_tools() suggests non-existent tool names "search", "code", "file"
- [ ] MEDIUM: _simple_expand() dictionary only has 9 entries — many short inputs pass through unchanged
- [ ] MEDIUM: _fallback_plan() duplicate time/date blocks — second block unreachable
- [ ] MEDIUM: _get_next_action() JSON repair ignores strings — brace counting wrong with `{}` in strings
- [ ] MEDIUM: Skill.from_markdown() never parses description — lost on reload from disk
- [ ] MEDIUM: improve_skill() doesn't update in-memory cache — stale cache after improvement
- [ ] MEDIUM: Skill step parsing too broad — matches any line containing "Step" anywhere
- [ ] MEDIUM: 0/0 success rate display in generated skills
- [ ] MEDIUM: SQL injection via f-string table name in local_db.py
- [ ] MEDIUM: _get_conn() race condition — no lock on connection creation
- [ ] MEDIUM: Broadcast bare except swallows CancelledError — prevents clean shutdown
- [ ] MEDIUM: No WebSocket ping/pong keepalive — stale clients accumulate
- [ ] MEDIUM: Faiss normalization on empty index may AttributeError (neural_memory.py:55)
- [ ] MEDIUM: Neural memory API key passed as URL query param — visible in logs (neural_memory.py:65)
- [ ] MEDIUM: SentenceTransformer model loaded at init — blocks event loop for 500ms+ (five_tier.py:369-392)
- [ ] MEDIUM: FAISS index rebuilt from scratch on every restart — slow for large memory (five_tier.py:394-441)
- [ ] MEDIUM: asyncio.run() called from sync context in voice module — RuntimeError in FastAPI
- [ ] MEDIUM: Orchestrator shell injection — prompt interpolated into CMD string (orchestrator.py:144-156)
- [ ] MEDIUM: alfred_assistant.py duplicate except blocks in check_calendar() (lines 206-207)
- [ ] MEDIUM: alfred_assistant.py asyncio.chat() blocks on sync OpenAI call
- [ ] MEDIUM: "add " self-coding prefix too broad — catches "add to my tasks" (alfred_assistant.py:676)
- [ ] MEDIUM: AI email fallback sends topic as body on failure (alfred_assistant.py:312-314)
- [ ] MEDIUM: Calendar ambiguity detection basic — only duplicate times, no partial name matching
- [ ] MEDIUM: GWSClient Drive search injection — f-string query (gws_client.py:498)
- [ ] MEDIUM: GWSClient credential refresh race condition — no lock (gws_client.py:126-130)
- [ ] MEDIUM: Cockpit CallInterface.tsx: SERVER_URL is empty string, /api/call endpoint doesn't exist
- [ ] MEDIUM: Loop_tests.py Test 8 always fails — mock output doesn't contain expected phrases

## LOW: Casual greeting matching misses "yo", "heya", "sup" — too strict
- [ ] LOW: Hardcoded Windows paths for bootstrap directory
- [ ] LOW: Inconsistent env var naming — OBSIDIAN_VAULT_PATH vs OBSIDIAN_VAULT
- [ ] LOW: 3 separate Python servers (brain_api 8001, voice 8002, daemon 8765) — no unified config
- [ ] LOW: Unused _estimate_tokens() method in alfred.py
- [ ] LOW: Message content truncated silently at 300 chars without indicator
- [ ] LOW: T4 profile overwrites existing keys silently — no user warning
- [ ] LOW: No X-Goog-Api-Key header — API key in URL query (neural_memory.py:65)
