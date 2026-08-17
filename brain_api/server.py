"""
Alfred Brain API - FastAPI Server v3.0

Fully local architecture:
- SQLite for user_state, conversations
- FAISS for neural memories (personal facts about Master Sam)
- Ngrok auto-management for remote access
- All LLM, tool, memory logic in Alfred brain

No Supabase. No external dependencies from Cockpit.
"""

import asyncio
import json
import os
import time
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Add parent to path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

# API keys MUST be set via environment variables (see .env.example)
# Never hardcode keys in this public repo
# Keys loaded via dotenv in brain/alfred.py and other modules

from brain_api.models import (
    ChatMessage,
    ChatResponse,
    StatusResponse,
    ContextResponse,
    SkillsResponse,
    SkillInfo,
    TasksResponse,
    TaskInfo,
    HealthResponse,
    AlfredStatus,
    PhaseInfo,
)
from brain import get_alfred, get_memory, get_skill_manager
from brain.local_db import get_local_db
from brain.neural_memory import get_neural_memory
from brain.tools.gws_client import get_auth_url, exchange_code, get_token_status, AuthRequiredError

# Global state
START_TIME = time.time()
CONNECTED_CLIENTS: Set[WebSocket] = set()
NGROK_URL: Optional[str] = None
NGROK_PROCESS: Optional[subprocess.Popen] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler for startup/shutdown."""
    print("=" * 50)
    print("  ALFRED BRAIN API v3.0")
    print("  Fully Local Architecture")
    print("=" * 50)
    print(f"  Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # Initialize brain
    alfred = get_alfred()
    memory = get_memory()
    
    # Initialize local DB and import from JSON exports
    db = get_local_db()
    data_dir = Path(__file__).parent.parent / "brain" / "data"
    for table in ["user_state", "conversations"]:
        json_path = data_dir / f"{table}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            db.import_json(table, data)
            print(f"  [DB] Imported {len(data)} rows from {table}.json")
        else:
            print(f"  [DB] No JSON export for {table}")
    
    state = db.get_user_state()
    print(f"  Local DB: mode={state.get('mode', 'FOUNDER')}")
    
    # Initialize neural memory
    nm = get_neural_memory()
    if nm.count() == 0:
        count = nm.import_supabase_export()
        print(f"  Neural Memory: {count} memories imported from JSON export")
    else:
        print(f"  Neural Memory: {nm.count()} memories indexed")
    
    # Start ngrok
    start_ngrok()
    
    print("  Alfred Brain initialized successfully")
    print("=" * 50)

    # Start alert broadcaster for reminders
    alert_task = asyncio.create_task(_alert_broadcaster())

    yield

    alert_task.cancel()
    print("\nShutting down Alfred Brain API...")
    stop_ngrok()


# ============ NGROK MANAGEMENT ============

def start_ngrok():
    """Start ngrok tunnel and capture URL."""
    global NGROK_PROCESS, NGROK_URL
    
    ngrok_token = sys.modules.get('os', __import__('os')).environ.get("NGROK_AUTH_TOKEN", "")
    if not ngrok_token:
        # Try to find ngrok.exe in parent directory
        ngrok_path = Path(__file__).parent.parent.parent / "ngrok.exe"
        if not ngrok_path.exists():
            print("  [ngrok] No ngrok.exe found, skipping")
            return
    else:
        ngrok_path = Path(__file__).parent.parent.parent / "ngrok.exe"
    
    if not ngrok_path.exists():
        print("  [ngrok] ngrok.exe not found")
        return
    
    try:
        # Start ngrok
        NGROK_PROCESS = subprocess.Popen(
            [str(ngrok_path), "http", "8001", "--log=stdout"],
            # No PIPE — avoid deadlock from full stdout buffer
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        
        # Read URL from ngrok API (not stdout parsing)
        import time as t
        t.sleep(2)  # Give ngrok time to start
        import requests
        try:
            resp = requests.get("http://localhost:4040/api/tunnels", timeout=5)
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        NGROK_URL = tunnel.get("public_url")
                        print(f"  [ngrok] Tunnel: {NGROK_URL}")
                        break
        except Exception as e:
            print(f"  [ngrok] Could not get URL from API: {e}")
        
        # Background thread to monitor ngrok URL
        def monitor_ngrok():
            global NGROK_URL
            while NGROK_PROCESS and NGROK_PROCESS.poll() is None:
                try:
                    import requests as rq
                    resp = rq.get("http://localhost:4040/api/tunnels", timeout=5)
                    if resp.status_code == 200:
                        tunnels = resp.json().get("tunnels", [])
                        for tunnel in tunnels:
                            if tunnel.get("proto") == "https":
                                new_url = tunnel.get("public_url")
                                if new_url != NGROK_URL:
                                    NGROK_URL = new_url
                                    print(f"  [ngrok] URL updated: {NGROK_URL}")
                                break
                except:
                    pass
                t.sleep(10)
        
        threading.Thread(target=monitor_ngrok, daemon=True).start()
        
    except Exception as e:
        print(f"  [ngrok] Failed to start: {e}")


def stop_ngrok():
    """Stop ngrok process."""
    global NGROK_PROCESS
    if NGROK_PROCESS and NGROK_PROCESS.poll() is None:
        NGROK_PROCESS.terminate()
        NGROK_PROCESS = None


# ============ FASTAPI APP ============

app = FastAPI(
    title="Alfred Brain API",
    description="API for Alfred - Autonomous AI Assistant (v3.0 Local Architecture)",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ WEBSOCKET HANDLING ============

async def broadcast_to_clients(message: dict):
    """Broadcast message to all connected WebSocket clients."""
    disconnected = set()
    for client in CONNECTED_CLIENTS:
        try:
            await asyncio.wait_for(client.send_json(message), timeout=2.0)
        except:
            disconnected.add(client)
    CONNECTED_CLIENTS.difference_update(disconnected)


async def _alert_broadcaster():
    """Periodically check for pending alerts and broadcast via WebSocket."""
    try:
        while True:
            await asyncio.sleep(5)
            alfred = get_alfred()
            alerts = alfred.pop_alerts()
            for alert in alerts:
                await broadcast_to_clients(alert)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[Alerts] Broadcaster error: {e}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time communication."""
    await websocket.accept()
    CONNECTED_CLIENTS.add(websocket)

    try:
        await websocket.send_json(
            {
                "type": "connected",
                "status": "idle",
                "message": "Connected to Alfred Brain v3.0",
            }
        )

        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type", "chat")

            if msg_type == "chat":
                response = await process_chat(
                    message.get("message", ""), message.get("session_id")
                )
                await websocket.send_json(response.model_dump() if hasattr(response, 'model_dump') else response.__dict__)
            elif msg_type == "ping":
                await websocket.send_json(
                    {"type": "pong", "timestamp": datetime.now().isoformat()}
                )

    except WebSocketDisconnect:
        CONNECTED_CLIENTS.discard(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        CONNECTED_CLIENTS.discard(websocket)


# ============ REST ENDPOINTS ============

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    memory = get_memory()
    db = get_local_db()
    nm = get_neural_memory()
    
    return HealthResponse(
        status="ok",
        version="3.0.0",
        uptime_seconds=int(time.time() - START_TIME),
        memory_tiers={
            "t1_context_items": len(memory.t1_get_all()),
            "t2_skills": len(memory._t2_skills_index),
            "neural_memories": nm.count(),
        },
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(message: ChatMessage):
    """Main chat endpoint."""
    return await process_chat(message.message, message.session_id)


@app.post("/api/command")
async def api_command(data: Dict):
    """Cockpit compatibility endpoint."""
    message = data.get("command", data.get("message", ""))
    session_id = data.get("session_id")
    result = await process_chat(message, session_id)
    return {
        "response": result.response,
        "thinking": result.thinking if hasattr(result, 'thinking') else []
    }


async def process_chat(
    user_message: str, session_id: Optional[str] = None
) -> ChatResponse:
    """Process a chat message through Alfred loop."""
    alfred = get_alfred()
    db = get_local_db()
    
    try:
        # Load history before persisting current message to avoid duplication
        history = db.get_recent_context(session_id, count=20) if session_id else []
        if session_id:
            db.add_message(session_id, "user", user_message)
        context = {"session_id": session_id, "conversation_history": history} if session_id else {"conversation_history": []}

        result = await alfred.execute(user_message, context)
        response_text = result.get("response", "Done.")

        # Persist assistant response
        if session_id:
            db.add_message(session_id, "assistant", response_text)

        # Auto-update session summary
        if session_id:
            try:
                session = db.get_session(session_id)
                if session:
                    prev_summary = session.get("summary", "")
                    new_entry = f"User: {user_message[:200]}\nAlfred: {response_text[:500]}"
                    if prev_summary:
                        updated = f"{prev_summary}\n\n{new_entry}"
                    else:
                        updated = new_entry
                    if len(updated) > 2000:
                        updated = updated[-2000:]
                    db.update_session(session_id, summary=updated)
                    db.touch_session(session_id)
            except Exception:
                pass

        return ChatResponse(
            response=response_text,
            status=AlfredStatus.IDLE,
            phase=PhaseInfo(
                current=result.get("phase", "completed"),
                step=1,
                total_steps=len(result.get("steps", [])),
                message="",
            ),
            skill_used=result.get("skill_used", False),
            skill_generated=result.get("skill_generated", False),
            steps=result.get("steps", []),
            session_id=session_id,
            thinking=result.get("thinking", []),
        )

    except Exception as e:
        return ChatResponse(
            response=f"I encountered an error: {str(e)}",
            status=AlfredStatus.ERROR,
            session_id=session_id,
        )


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """Get Alfred's current status."""
    memory = get_memory()
    db = get_local_db()
    state = db.get_user_state()
    
    return StatusResponse(
        status=AlfredStatus.IDLE,
        current_task=memory.t1_get("current_task"),
        phase="idle",
        uptime_seconds=int(time.time() - START_TIME),
        memory_stats={
            "t1_items": len(memory.t1_get_all()),
            "mode": state.get("mode", "FOUNDER"),
        },
        skills_count=len(memory._t2_skills_index),
    )


@app.get("/context", response_model=ContextResponse)
async def get_context():
    """Get context data for UI panels."""
    memory = get_memory()
    db = get_local_db()
    state = db.get_user_state()
    
    t1_context = memory.t1_get_all()
    skills = memory._t2_skills_index[:5]
    skills_list = [s.get("title", "Unknown") for s in skills]

    return ContextResponse(
        current_task=t1_context.get("current_task"),
        recent_messages=[],
        tasks=[],
        calendar=[],
        weather=None,
        skills_available=skills_list,
    )


@app.get("/skills", response_model=SkillsResponse)
async def get_skills():
    """Get all available skills."""
    memory = get_memory()
    skills = []
    for skill in memory._t2_skills_index:
        skills.append(
            SkillInfo(
                id=skill.get("skill_id", ""),
                title=skill.get("title", "Unknown Skill"),
                complexity="medium",
                success_rate=0.0,
                tags=skill.get("tags", []),
            )
        )
    return SkillsResponse(skills=skills, total=len(skills))


@app.get("/tasks", response_model=TasksResponse)
async def get_tasks():
    """Get all tasks."""
    return TasksResponse(tasks=[], total=0)


# ============ LOCAL DB ENDPOINTS (Replaces Supabase) ============

@app.get("/api/state")
async def get_state():
    """Get current user state (mode, telemetry, etc.)."""
    db = get_local_db()
    return db.get_user_state()


@app.put("/api/state")
async def update_state(data: Dict):
    """Update user state (mode, telemetry, etc.)."""
    db = get_local_db()
    result = db.update_user_state(**data)
    
    # Broadcast mode change to WebSocket clients
    if "mode" in data:
        await broadcast_to_clients({
            "type": "mode_change",
            "mode": data["mode"],
        })
    
    return result


@app.get("/api/sessions")
async def get_sessions(
    limit: int = Query(20, le=100),
    active_only: bool = Query(True),
):
    """Get conversation sessions."""
    db = get_local_db()
    sessions = db.get_sessions(limit=limit, active_only=active_only)
    return {"sessions": sessions, "total": len(sessions)}


@app.post("/api/sessions")
async def create_session(data: Dict):
    """Create a new conversation session."""
    db = get_local_db()
    session_id = db.create_session(
        session_id=data.get("session_id"),
        session_name=data.get("session_name", ""),
    )
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session with its summary."""
    db = get_local_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/api/sessions/{session_id}/summary")
async def get_session_summary(session_id: str):
    """Get a session's auto-generated summary."""
    db = get_local_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "summary": session.get("summary", "")}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """Get messages for a session."""
    db = get_local_db()
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    messages = db.get_messages(session_id, limit=limit, offset=offset)
    total = db.get_message_count(session_id)
    return {"messages": messages, "total": total}


@app.get("/api/sessions/{session_id}/episodes")
async def get_session_episodes(session_id: str):
    """Get episode paths for a session."""
    db = get_local_db()
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    episodes = db.get_session_episodes(session_id)
    return {"episodes": episodes, "total": len(episodes)}


@app.put("/api/sessions/{session_id}")
async def update_session(session_id: str, data: Dict):
    """Update a session (name, summary, active status)."""
    db = get_local_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.update_session(session_id, **data)
    return db.get_session(session_id)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and its messages."""
    db = get_local_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete_session_messages(session_id)
    db.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}


# ============ NEURAL MEMORY ENDPOINTS (Replaces Supabase neural_memories) ============

@app.get("/api/memories")
async def search_memories(
    query: str = Query(""),
    category: str = Query(None),
    top_k: int = Query(5, le=20),
):
    """Search neural memories (semantic or by category)."""
    nm = get_neural_memory()
    
    if query:
        results = nm.recall(query, top_k=top_k)
    elif category:
        results = nm.search_by_category(category, top_k=top_k)
    else:
        results = nm.get_all(limit=top_k)
    
    return {"memories": results, "total": len(results)}


@app.post("/api/memories")
async def store_memory(data: Dict):
    """Store a new neural memory."""
    nm = get_neural_memory()
    success = nm.add(
        content=data.get("content", ""),
        category=data.get("category", "general"),
        metadata=data.get("metadata", {}),
    )
    return {"success": success, "count": nm.count()}


@app.get("/api/memories/stats")
async def memory_stats():
    """Get neural memory statistics."""
    nm = get_neural_memory()
    
    # Count by category
    all_memories = nm.get_all(limit=10000)
    categories = {}
    for m in all_memories:
        cat = m.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
    
    return {
        "total": nm.count(),
        "by_category": categories,
    }


# ============ NGROK ENDPOINTS ============

@app.get("/api/ngrok-url")
async def get_ngrok_url():
    """Get current ngrok tunnel URL for Cockpit auto-discovery."""
    global NGROK_URL
    return {
        "url": NGROK_URL,
        "local_url": "http://localhost:8001",
    }


# ============ GWS AUTH ENDPOINTS ============

@app.get("/api/auth/gws")
async def gws_auth_status():
    """Check GWS OAuth authentication status."""
    return get_token_status()


@app.post("/api/auth/gws/login")
async def gws_auth_login():
    """Get OAuth authorization URL for headless auth flow."""
    try:
        auth_url = get_auth_url()
        return {
            "auth_url": auth_url,
            "instructions": "Visit the URL in a browser, grant access, then POST the code to /api/auth/gws/callback as {\"code\": \"...\"}",
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/gws/callback")
async def gws_auth_callback(data: dict):
    """Exchange authorization code for token."""
    code = data.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' in request body")
    try:
        result = exchange_code(code)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Code exchange failed: {e}")


# ============ MAIN ============

def run_server(host: str = "127.0.0.1", port: int = None):
    if port is None:
        port = int(os.environ.get("PORT", "8001"))
    """Run the Alfred Brain API server."""
    print(f"\nStarting Alfred Brain API v3.0 on {host}:{port}")
    print(f"WebSocket: ws://{host}:{port}/ws")
    print(f"API Docs: http://{host}:{port}/docs\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
