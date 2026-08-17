# Alfred v2 — Sovereign AGI Assistant

> **Master Sam's autonomous AI assistant** — A modular, self-evolving personal agent running locally with voice, memory, and proactive intelligence.

**Status**: Phase 1 (Sovereign Core) — In Development

## 🚀 Quick Links

- **[Quick Start Guide](SETUP.md)** — Get Alfred running in 5 minutes
- **[Architecture Overview](ARCHITECTURE.md)** — How the v2 system works
- **[Development Guide](CONTRIBUTING.md)** — Extend Alfred with new tools/skills
- **[Vision & Roadmap](docs/PHASES.md)** — 5-phase evolution to AGI
- **[Full Manifesto](docs/MANIFESTO_V5.md)** — The complete vision document

---

## What is Alfred?

Alfred is a **locally-running AI assistant** built on a Hermes-inspired modular architecture. Unlike cloud-dependent chatbots, Alfred runs on your machine with:

### ✨ Core Capabilities
- **30+ Tools**: Calendar (Google), Email (Gmail), Web Search, File I/O, Shell, Weather, Time, Calculator, Reminders, Memory CRUD, and more
- **5-Tier Memory System**: 
  - T1: Short-term context (current conversation)
  - T2: Procedural skills (auto-generated workflows)
  - T3: Episodic memory (hybrid FAISS/BM25 search)
  - T4: User profile (persistent key-value store)
  - T5: Full-text archive (SQLite FTS5)
- **Cognitive Heartbeat**: Background loop that checks reminders, runs cron tasks, performs calendar/email triage every 30 minutes
- **Self-Improvement**: Automatically generates skills (T2) for complex multi-tool tasks, learns from experience
- **Voice Pipeline**: Faster-Whisper STT + Edge-TTS running locally
- **Modular Architecture**: Clean separation of concerns — easy to extend, test, and maintain

### 📊 Current Status
| Component | Status | Tests |
|-----------|--------|-------|
| PromptBuilder | ✅ Complete | 6/6 |
| ContextManager | ✅ Complete | 10/10 |
| LLMRouter | ✅ Complete | 7/7 |
| ToolExecutor | 🚀 In Progress | — |
| Conversation Loop | 🚀 In Progress | — |
| Heartbeat | 🚀 In Progress | — |

---

## 🏗️ Phase 1: Sovereign Core (Weeks 1-2)

**Goal**: Replace reactive behavior with memory-driven, self-verifying execution.

### Completed (Days 1-3)
- ✅ Project scaffolding & v2 architecture
- ✅ PromptBuilder: token-budgeted system prompt assembly
- ✅ ContextManager: conversation history with compression
- ✅ LLMRouter: adaptive multi-provider failover (Groq → OpenRouter → Gemini)

### In Progress (Days 4-10)
- **Day 4**: ToolExecutor core (dispatch, guardrails, mutation verification)
- **Day 5**: Wire in tool handlers (time, calculator, calendar, email, web_search, shell, file I/O, memory, reminders)
- **Day 6**: Conversation loop (LLM→tool→LLM with turn limits and compression)
- **Day 7**: Heartbeat (proactive reasoning + cron execution)
- **Day 8**: Wire public API (brain_api/server.py integration)
- **Day 9**: Run full test suites & fix failures (target: 100% pass)
- **Day 10**: Token budget tuning & final polish

### Test Baselines
```
phase1_d1d2_test.py:     10/13 PASS (3 timeout failures)
phase1_cockpit_test.py:  13/20 PASS (7 failures on complex goals)
```

---

## 📖 Key Documents

| Document | Purpose |
|----------|---------|
| [SETUP.md](SETUP.md) | Installation, env vars, running tests & server |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module breakdown, component design, data flow |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev workflow, TDD process, adding tools |
| [docs/PHASES.md](docs/PHASES.md) | Phase 1-5 roadmap with detailed checklist |
| [docs/MANIFESTO_V5.md](docs/MANIFESTO_V5.md) | Complete vision: sovereignty, memory, self-evolution |

---

## ⚡ Quick Examples

### Command Line (One-Shot)
```bash
python -c "
import asyncio
from brain.v2 import execute_task
result = asyncio.run(execute_task('What time is it?'))
print(result['response'])
"
```

### Server + API
```bash
# Start server
python -m brain_api.server

# In another terminal
curl http://localhost:8001/api/health

# Execute a task
curl -X POST http://localhost:8001/api/execute \
  -H "Content-Type: application/json" \
  -d '{"task": "What are my upcoming calendar events?"}'
```

### WebSocket (Real-Time)
```bash
# Connect to WebSocket at ws://localhost:8001/ws
# Send: {"task": "Set a reminder for tomorrow at 9am"}
# Receive: {"response": "...", "thinking": [...], "tools_called": [...]}
```

---

## 🛠️ Development

### Run Tests
```bash
# Unit tests (per module)
python build-system/test_prompt_builder.py
python build-system/test_context_manager.py
python build-system/test_llm_router.py

# Integration tests
python build-system/phase1_d1d2_test.py
python build-system/phase1_cockpit_test.py
```

### Add a New Tool
1. Write handler function in `tool_executor.py` (or new file)
2. Register in `create_tool_executor()`
3. Add schema to `_get_tool_descriptions()` in `conversation.py`
4. Write unit test
5. Run integration tests to verify

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed TDD workflow.

---

## 📁 Project Structure

```
alfred/
├── README.md                          # This file
├── SETUP.md                          # Installation & quickstart
├── ARCHITECTURE.md                   # System design
├── CONTRIBUTING.md                   # Development guide
│
├── brain/                            # Core Alfred logic
│   ├── v2/                          # Hermes-inspired modular loop
│   │   ├── __init__.py              # Public API exports
│   │   ├── prompt_builder.py        # System prompt assembly
│   │   ├── context_manager.py       # Conversation history
│   │   ├── tool_executor.py         # Tool dispatch & guardrails
│   │   ├── conversation.py          # Main LLM→tool→LLM loop
│   │   └── heartbeat.py             # Cognitive heartbeat
│   ├── memory/                      # 5-tier memory system
│   ├── tools/                       # Concrete tool implementations
│   ├── llm_router.py               # Multi-provider LLM failover
│   ├── local_db.py                 # SQLite persistence
│   └── __init__.py
│
├── brain_api/                        # FastAPI REST/WebSocket server
│   ├── server.py                    # Main server
│   └── models.py                    # Pydantic request/response models
│
├── build-system/                     # Testing & CI/CD
│   ├── test_prompt_builder.py
│   ├── test_context_manager.py
│   ├── test_llm_router.py
│   ├── phase1_d1d2_test.py
│   ├── phase1_cockpit_test.py
│   └── PROJECT_TRACKER.md
│
├── docs/                             # Documentation
│   ├── MANIFESTO_V5.md              # Vision & 5-phase roadmap
│   ├── PHASES.md                    # Phase-by-phase checklist
│   ├── API.md                       # REST/WebSocket API spec
│   ├── TOOLS.md                     # Tool development guide
│   └── MEMORY.md                    # Memory system architecture
│
├── requirements.txt                  # Python dependencies
├── .env.example                      # Environment template
├── .gitignore                        # Git ignore rules
└── .claude/
    └── settings.json                # Claude Code configuration
```

---

## 🔧 Configuration

### Environment Variables (`.env`)
```bash
# LLM Providers
GROQ_API_KEY=your_key
GOOGLE_API_KEY=your_key
OPENROUTER_API_KEY=your_key

# Paths
OBSIDIAN_VAULT_PATH=C:\path\to\vault

# Optional
NGROK_AUTH_TOKEN=your_token
```

See `.env.example` for all available options.

---

## 🌟 Highlights of Phase 1

When complete, Alfred will:
1. **Remember skills** — T2 skills matched and reused without re-planning
2. **Verify mutations** — After creating calendar events or sending emails, automatically reads back to confirm success
3. **Proactive cognition** — Heartbeat runs every 30 minutes to check goals, calendar, inbox, and suggest actions
4. **Self-correct** — LLM classifier handles nuanced replies (acknowledgment, clarification, refusal)
5. **Persist learning** — Episodes saved to T3 for future reasoning, skills versioned and improved

---

## 🐛 Known Limitations

See [docs/PHASES.md](docs/PHASES.md) for full bug list. High-priority items:
- [ ] API key validation (currently hardcoded in some paths)
- [ ] Calculator uses bare `eval()` (RCE risk in untrusted environments)
- [ ] SQL injection in local_db.py (f-string table names)
- [ ] Goal inference currently disabled

---

## 📚 Further Reading

- **For Users**: Start with [SETUP.md](SETUP.md) to get running
- **For Developers**: Read [ARCHITECTURE.md](ARCHITECTURE.md) then [CONTRIBUTING.md](CONTRIBUTING.md)
- **For Vision**: Read [docs/MANIFESTO_V5.md](docs/MANIFESTO_V5.md)
- **For Tracking**: See [docs/PHASES.md](docs/PHASES.md) for progress checklist

---

## 👤 Author

**Master Sam (Vanaxity)** — AI researcher, builder, vision-driver

**Last Updated**: 2026-08-17  
**Current Phase**: 1 (Sovereign Core) — In Progress  
**Target Completion**: 2026-09-01
