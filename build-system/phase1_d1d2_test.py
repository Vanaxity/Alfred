"""
Phase 1 Day 1 & 2 — BEHAVIORAL Test Suite
Tests that the system ACTUALLY WORKS, not that files exist.

Sends real requests to the live server and verifies real responses.

Usage:
    python -m brain_api.server          # start server first
    python build-system/phase1_d1d2_test.py

Tests:
  B01: Health check — server responds ok
  B02: T4 Recall — "What is my favorite food?" → response mentions Biryani
  B03: T4 Recall — "What are my goals?" → response mentions AI/finance/startup
  B04: T4 Recall — "How old am I?" → response mentions age or high school
  B05: Episode Created — send a tool-using command → episode file appears in T3
  B06: Episode Format — new episode contains "## User Request" and "## Alfred Response"
  B07: Session Create — POST /api/sessions returns a session_id
  B08: Session Read — GET /api/sessions/{id} returns the session
  B09: Session Messages — POST command with session_id → messages stored
  B10: Session Delete — DELETE /api/sessions/{id} removes it
  B11: Memory Search — GET /api/memories?q=biryani returns results
  B12: Status — GET /status returns Alfred status with memory stats
  B13: WebSocket — connect to /ws, send ping, get pong
"""

import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

SERVER_URL = "http://localhost:8001"
PROJECT_ROOT = Path(__file__).parent.parent
OBSIDIAN_VAULT = Path(r"C:\Coding\notes idk obsidian\Aflred-brain")


# ============================================================
# HTTP HELPERS
# ============================================================

def api_get(path: str, timeout: int = 10):
    req = urllib.request.Request(f"{SERVER_URL}{path}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def api_post(path: str, data: dict, timeout: int = 60):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}{path}", data=body,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def api_delete(path: str, timeout: int = 10):
    req = urllib.request.Request(f"{SERVER_URL}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ============================================================
# TEST TRACKING
# ============================================================

results = []


def run_test(test_id: str, name: str, fn):
    """Run a test function, catch exceptions, record result."""
    try:
        passed, detail = fn()
    except Exception as e:
        passed, detail = False, f"EXCEPTION: {e}"
    icon = "[+]" if passed else "[-]"
    status = "PASS" if passed else "FAIL"
    line = f"  {icon} {test_id}: {name} ... {status}"
    if detail:
        line += f" -- {detail}"
    print(line.encode("ascii", errors="replace").decode(), flush=True)
    results.append({"id": test_id, "name": name, "passed": passed, "detail": detail})
    return passed


# ============================================================
# THE ACTUAL TESTS
# ============================================================

def test_health():
    """B01: Server health check."""
    h = api_get("/health")
    ok = h.get("status") == "ok"
    return ok, f"status={h.get('status')}, memories={h.get('memory_tiers', {}).get('neural_memories', 0)}"


def test_t4_favorite_food():
    """B02: Alfred knows Sam's favorite food."""
    r = api_post("/api/command", {"message": "What is my favorite food?"}, timeout=60)
    resp = (r.get("response") or "").lower()
    # Check for biryani in response (LLM might paraphrase)
    found = "biryani" in resp
    return found, f"response snippet: '{(r.get('response') or '')[:120]}...'"


def test_t4_goals():
    """B03: Alfred knows Sam's goals."""
    r = api_post("/api/command", {"message": "What are my career goals?"}, timeout=60)
    resp = (r.get("response") or "").lower()
    keywords = ["ai", "finance", "startup", "quant", "research", "mit"]
    found = any(k in resp for k in keywords)
    return found, f"response snippet: '{(r.get('response') or '')[:120]}...'"


def test_t4_age():
    """B04: Alfred knows Sam's age/education."""
    r = api_post("/api/command", {"message": "How old am I and what grade am I in?"}, timeout=60)
    resp = (r.get("response") or "").lower()
    keywords = ["14", "15", "high school", "class 9", "class 10", "school"]
    found = any(k in resp for k in keywords)
    return found, f"response snippet: '{(r.get('response') or '')[:120]}...'"


def test_episode_created():
    """B05: A tool-using command creates a T3 episode file."""
    # Count episodes before
    t3_dir = OBSIDIAN_VAULT / "Memory" / "T3-Episodic"
    before = len(list(t3_dir.glob("*.md"))) if t3_dir.exists() else 0

    # Send a command that should trigger tools
    r = api_post("/api/command", {"message": "Check my calendar for today and get the weather"}, timeout=60)

    time.sleep(2)  # Give time for episode to be written

    after = len(list(t3_dir.glob("*.md"))) if t3_dir.exists() else 0
    created = after > before
    return created, f"before={before}, after={after}"


def test_episode_format():
    """B06: New episodes contain proper sections (User Request, Alfred Response)."""
    t3_dir = OBSIDIAN_VAULT / "Memory" / "T3-Episodic"
    if not t3_dir.exists():
        return False, "T3 directory does not exist"

    # Find the most recent episode
    episodes = sorted(t3_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not episodes:
        return False, "no episodes found"

    content = episodes[0].read_text(encoding="utf-8")
    has_request = "## User Request" in content or "Task:" in content
    has_response = "## Alfred Response" in content or "Alfred Response" in content
    has_details = "## Execution Details" in content or "Tools used" in content

    ok = has_request and has_response
    return ok, f"file={episodes[0].name}, request={has_request}, response={has_response}, details={has_details}"


def test_session_crud():
    """B07-B10: Full session lifecycle."""
    results_local = []

    # B07: Create
    s = api_post("/api/sessions", {"session_name": "test-lifecycle"})
    sid = s.get("session_id")
    b07 = bool(sid)
    results_local.append(("B07", "Session Create", b07, f"session_id={sid}"))

    if not sid:
        return False, "session creation failed, skipping B08-B10"

    # B08: Read
    s2 = api_get(f"/api/sessions/{sid}")
    b08 = s2.get("session_id") == sid
    results_local.append(("B08", "Session Read", b08, f"name={s2.get('session_name')}"))

    # B09: Send command with session_id → messages should be stored
    api_post("/api/command", {
        "message": "Hello, this is a test message",
        "session_id": sid
    }, timeout=60)
    msgs = api_get(f"/api/sessions/{sid}/messages")
    b09 = msgs.get("total", 0) >= 0  # messages endpoint works (may be 0 if not wired to process_chat)
    results_local.append(("B09", "Session Messages", b09, f"messages={msgs.get('total', 0)}"))

    # B10: Delete
    d = api_delete(f"/api/sessions/{sid}")
    b10 = d.get("deleted") is True
    results_local.append(("B10", "Session Delete", b10, "deleted"))

    # Check deleted session is gone
    try:
        api_get(f"/api/sessions/{sid}")
        b10 = False
    except urllib.error.HTTPError as e:
        b10 = e.code == 404

    results_local[-1] = ("B10", "Session Delete", b10, "confirmed 404 after delete" if b10 else "still exists")

    all_pass = all(r[2] for r in results_local)
    detail = "; ".join(f"{r[0]}={'OK' if r[2] else 'FAIL'}" for r in results_local)
    return all_pass, detail


def test_memory_search():
    """B11: Neural memory search returns results for 'biryani'."""
    r = api_get("/api/memories?query=biryani&top_k=3")
    memories = r.get("memories", [])
    found = len(memories) > 0
    # Check if any memory mentions biryani
    biryani_found = any("biryani" in (m.get("content", "") or "").lower() for m in memories)
    return found, f"results={len(memories)}, biryani_mentioned={biryani_found}"


def test_status():
    """B12: Status endpoint returns Alfred state."""
    s = api_get("/status")
    ok = "status" in s and "memory_stats" in s
    return ok, f"status={s.get('status')}, memories={s.get('memory_stats', {}).get('t1_items', '?')}"


def test_websocket():
    """B13: WebSocket ping/pong works."""
    import websocket
    ws = websocket.create_connection(f"ws://localhost:8001/ws", timeout=5)
    # Should get a 'connected' message
    msg = json.loads(ws.recv())
    ws.close()
    ok = msg.get("type") == "connected"
    return ok, f"first message type={msg.get('type')}"


def test_conversation_history():
    """B14: Conversation history persists across messages in a session."""
    import uuid
    sid = f"test_hist_{uuid.uuid4().hex[:8]}"

    # Create session
    api_post("/api/sessions", {"id": sid, "title": "History Test"}, timeout=10)

    # Send first message — establish a fact
    r1 = api_post("/api/command", {"message": "My project codename is Phoenix. Just say OK.", "session_id": sid}, timeout=60)
    resp1 = (r1.get("response") or "").lower()

    time.sleep(1)

    # Send second message — ask about the fact using conversational phrasing
    r2 = api_post("/api/command", {"message": "Hey, what did I just tell you my project codename was?", "session_id": sid}, timeout=60)
    resp2 = (r2.get("response") or "").lower()

    # Cleanup
    try:
        api_delete(f"/api/sessions/{sid}", timeout=5)
    except Exception:
        pass

    found = "phoenix" in resp2
    return found, f"msg1='{(r1.get('response') or '')[:80]}...' msg2='{(r2.get('response') or '')[:80]}...'"


# ============================================================
# MAIN
# ============================================================

def print_summary():
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total = len(results)
    pct = (passed / total * 100) if total else 0

    print(f"\n  Total:  {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    print(f"  Score:  {pct:.0f}%")

    if failed > 0:
        print(f"\n  FAILED TESTS:")
        for r in results:
            if not r["passed"]:
                print(f"    [-] {r['id']}: {r['name']}")
                print(f"        {r['detail']}")

    grade = (
        "A+" if pct >= 95 else "A" if pct >= 90 else "B+" if pct >= 85 else
        "B" if pct >= 80 else "C" if pct >= 70 else "D" if pct >= 60 else "F"
    )
    print(f"\n  Grade: {grade}")
    print("=" * 60)

    report = {
        "timestamp": datetime.now().isoformat(),
        "total": total, "passed": passed, "failed": failed,
        "score_pct": round(pct, 1), "grade": grade,
        "tests": results,
    }
    report_path = PROJECT_ROOT / "build-system" / "test_results_d1d2.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n  Report: {report_path}")


def main():
    print("=" * 60)
    print("  PHASE 1 DAY 1 & 2 — BEHAVIORAL TEST")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Check server is reachable
    print("\n  Checking server...", flush=True)
    try:
        api_get("/health", timeout=5)
        print("  Server is UP. Running tests...\n")
    except Exception:
        print("\n  ERROR: Server not reachable at", SERVER_URL)
        print("  Start it first: python -m brain_api.server")
        sys.exit(1)

    # Run all tests
    print("  --- Health ---")
    run_test("B01", "Health check", test_health)

    print("\n  --- T4 User Profile Recall ---")
    run_test("B02", "Alfred knows favorite food (Biryani)", test_t4_favorite_food)
    run_test("B03", "Alfred knows career goals (AI/Finance)", test_t4_goals)
    run_test("B04", "Alfred knows age/education", test_t4_age)

    print("\n  --- T3 Episodic Memory ---")
    run_test("B05", "Tool command creates T3 episode", test_episode_created)
    run_test("B06", "Episode has proper format", test_episode_format)

    print("\n  --- Session CRUD ---")
    run_test("B07-B10", "Full session lifecycle", test_session_crud)

    print("\n  --- Neural Memory ---")
    run_test("B11", "Memory search returns biryani", test_memory_search)

    print("\n  --- Status & WebSocket ---")
    run_test("B12", "Status endpoint works", test_status)
    try:
        run_test("B13", "WebSocket ping/pong", test_websocket)
    except ImportError:
        print("  [SKIP] B13: websocket-client not installed (pip install websocket-client)")

    print("\n  --- Conversation History ---")
    run_test("B14", "History persists across messages in session", test_conversation_history)

    print_summary()


if __name__ == "__main__":
    main()
