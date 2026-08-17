# Setup & Installation Guide

Get Alfred running in 5 minutes.

---

## 📋 Prerequisites

- **Python 3.10+** (check: `python --version`)
- **pip** (comes with Python)
- **Git** (optional, but recommended)
- **API Keys** (Groq, Google, OpenRouter — free tiers available)

---

## ⚡ Quick Start (5 minutes)

### 1. Clone/Download Alfred

```bash
# If using git
git clone https://github.com/your-org/alfred.git
cd alfred

# Or if you already have it
cd C:\Coding\alfred
```

### 2. Create Virtual Environment

```bash
# Create venv
python -m venv .venv

# Activate (Windows)
.\.venv\Scripts\activate

# Activate (macOS/Linux)
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

**Takes 2-3 minutes.** You'll see:
```
Successfully installed asyncio-contextmanager, fastapi, uvicorn, ...
```

### 4. Configure Environment

```bash
# Copy template
cp .env.example .env

# Edit .env with your API keys
# nano .env    (or use your editor)
```

**Minimum required**:
```bash
GROQ_API_KEY=your_groq_api_key_here
GOOGLE_API_KEY=your_google_api_key_here
OPENROUTER_API_KEY=your_openrouter_api_key_here
OBSIDIAN_VAULT_PATH=C:\path\to\your\vault
```

**Get free API keys**:
- [Groq Console](https://console.groq.com) — Free tier, very fast
- [Google Cloud](https://console.cloud.google.com) — Enable Gemini API
- [OpenRouter](https://openrouter.ai) — Free trial credits

### 5. Verify Installation

```bash
# Test imports
python -c "from brain.v2 import Alfred, get_alfred; print('✓ Alfred imported successfully')"

# Run a simple test
python build-system/test_prompt_builder.py
# Should see: 6/6 PASS
```

---

## 🧪 Running Tests

### Unit Tests (Per Module)

```bash
# Test prompt builder (token budgeting)
python build-system/test_prompt_builder.py
# Expected: 6/6 PASS

# Test conversation history (compression)
python build-system/test_context_manager.py
# Expected: 10/10 PASS

# Test LLM router (multi-provider failover)
python build-system/test_llm_router.py
# Expected: 7/7 PASS
```

### Integration Tests

**Before running**: Start the server in another terminal (see below).

```bash
# Phase 1 Day 1-2 behavioral tests
python build-system/phase1_d1d2_test.py
# Expected: 10/13 PASS (3 timeout failures are known)

# Cockpit-style conversational tests
python build-system/phase1_cockpit_test.py
# Expected: 13/20 PASS (7 failures are known)
```

**Interpreting results**:
```
[+] B01: Health check                                       PASS
[+] B02: T4 Recall (favorite food)                          PASS
[-] B03: T4 Recall (goals)                                  FAIL (T4 not injected yet)
[+] B07: Session Create                                     PASS
```

---

## 🚀 Running Alfred

### Option 1: One-Shot CLI

```bash
python -c "
import asyncio
from brain.v2 import execute_task

result = asyncio.run(execute_task('What time is it?'))
print('Response:', result['response'])
print('Tools:', result['tools_called'])
"
```

**Output**:
```
Response: It's currently 2:30 PM.
Tools: ['time']
```

### Option 2: Server + REST API

**Terminal 1** — Start the server:
```bash
python -m brain_api.server
```

**Output**:
```
[2026-08-17 14:30] Starting FastAPI server...
[2026-08-17 14:30] Uvicorn running on http://localhost:8001
[2026-08-17 14:30] Docs at http://localhost:8001/docs
```

**Terminal 2** — Make requests:

```bash
# Health check
curl http://localhost:8001/api/health

# Execute a task
curl -X POST http://localhost:8001/api/execute \
  -H "Content-Type: application/json" \
  -d '{"task": "What are my upcoming events?"}'

# Response:
{
  "response": "You have 3 upcoming events...",
  "thinking": ["[LLM provider=groq]", "[Turn 1] Tool: calendar", ...],
  "tools_called": ["calendar"],
  "tool_results": [{"tool": "calendar", "output": "...", "success": true}]
}
```

### Option 3: WebSocket (Real-Time Streaming)

```bash
# Using websocat (install: cargo install websocat)
websocat ws://localhost:8001/ws

# Send:
{"task": "Set a reminder for tomorrow at 9am"}

# Receive:
{"response": "Done!", "tools_called": ["set_reminder"], ...}
```

---

## 🔌 API Endpoints

### REST

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | Server health check |
| `/api/execute` | POST | Execute a task (async) |
| `/api/sessions` | POST | Create a new session |
| `/api/sessions/{id}` | GET | Get session details |
| `/api/sessions/{id}` | DELETE | Delete a session |
| `/api/memories` | GET | Search memories |
| `/status` | GET | Server status & stats |

### WebSocket

| Endpoint | Purpose |
|----------|---------|
| `/ws` | Real-time task execution & alerts |

See `docs/API.md` for full spec (coming soon).

---

## ⚙️ Configuration

### Environment Variables (`.env`)

```bash
# ===== LLM Providers =====
GROQ_API_KEY=gsk_xxxx              # Groq (primary)
GOOGLE_API_KEY=AIzaSyxxxx           # Gemini (fallback 2)
OPENROUTER_API_KEY=sk-or-xxxx       # OpenRouter (fallback 1)

# ===== Paths =====
OBSIDIAN_VAULT_PATH=C:\Users\samra\Documents\ObsidianVault

# ===== Optional =====
NGROK_AUTH_TOKEN=your_token         # For remote access
DEBUG=true                          # Enable debug logs
LOG_LEVEL=INFO                      # DEBUG, INFO, WARNING, ERROR
```

### Constants in Code

**Edit `brain/v2/conversation.py`** for tuning:
```python
class Alfred:
    CHAT_MODEL = "llama-3.1-8b-instant"   # Default LLM model
    MAX_TURNS = 10                        # Max loop iterations
    CONVERSATION_BUDGET = 12000           # Max conversation tokens
    SYSTEM_PROMPT_BUDGET = 8000           # Max system prompt tokens
```

**Edit `brain/v2/heartbeat.py`** for heartbeat:
```python
class CognitiveHeartbeat:
    DEFAULT_INTERVAL = 1800  # 30 minutes (in seconds)
```

---

## 🐛 Troubleshooting

### "ModuleNotFoundError: No module named 'groq'"

**Solution**: Make sure you activated the venv and installed requirements.
```bash
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### "GROQ_API_KEY not found"

**Solution**: Create/edit `.env` file in the project root:
```bash
GROQ_API_KEY=your_actual_key
```

Then test:
```bash
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.environ.get('GROQ_API_KEY'))"
```

### "Connection refused: localhost:8001"

**Solution**: Start the server first (in another terminal):
```bash
python -m brain_api.server
```

### Tests timeout or fail

**Common causes**:
1. API keys invalid → verify in `.env`
2. Network issue → check internet connection
3. Rate-limited → wait a minute, try again
4. LLM provider down → switch to fallback (already automatic)

**Debug**:
```bash
# Run with verbose logging
export LOG_LEVEL=DEBUG
python build-system/phase1_d1d2_test.py
```

### "ImportError: cannot import name 'Alfred' from 'brain.v2'"

**Solution**: Verify `.env` is loaded correctly and you're in the right directory:
```bash
cd C:\Coding\alfred
python -c "from brain.v2 import Alfred; print(Alfred)"
```

---

## 📚 Next Steps

1. **Run the tests** — Verify everything works
2. **Read [ARCHITECTURE.md](ARCHITECTURE.md)** — Understand the design
3. **Read [CONTRIBUTING.md](CONTRIBUTING.md)** — Start developing
4. **Try examples** — Play with the API

---

## 🚀 Development Workflow

If you're contributing (Day 4 onwards):

1. **Create a branch**
   ```bash
   git checkout -b feature/day4-tool-executor
   ```

2. **Write failing test first** (TDD)
   ```bash
   python build-system/test_tool_executor.py
   # Should FAIL (not implemented yet)
   ```

3. **Implement the feature**
   - Edit `brain/v2/tool_executor.py`
   - Add exports to `brain/v2/__init__.py`

4. **Run tests**
   ```bash
   python build-system/test_tool_executor.py
   # Should PASS
   ```

5. **Commit**
   ```bash
   git add .
   git commit -m "feat: implement ToolExecutor core dispatch"
   ```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full TDD walkthrough.

---

## 📖 Learn More

- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Development**: [CONTRIBUTING.md](CONTRIBUTING.md)
- **Roadmap**: [docs/PHASES.md](docs/PHASES.md)
- **Vision**: [docs/MANIFESTO_V5.md](docs/MANIFESTO_V5.md)

---

**Last Updated**: 2026-08-17  
**Status**: Phase 1 (Sovereign Core) — In Progress
