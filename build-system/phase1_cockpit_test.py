"""
Phase 1 Cockpit Integration Test
=================================
Sends 20 real questions to Alfred via POST /api/command and evaluates
whether Phase 1 features actually work in practice.

Usage:
    1. Start Alfred server:  python project-alfred/brain_api/server.py
    2. Run this test:        python project-alfred/build-system/phase1_cockpit_test.py

Requirements:
    - Alfred server running on localhost:8001
    - GWS OAuth authenticated (for calendar/email tests)
"""

import requests
import json
import sys
import time
import re
from dataclasses import dataclass, field
from typing import List, Optional, Callable

# ── Config ──────────────────────────────────────────────────────────────────

BASE_URL = "http://localhost:8001"
COMMAND_ENDPOINT = f"{BASE_URL}/api/command"
HEALTH_ENDPOINT = f"{BASE_URL}/health"
SESSION_ID = "phase1_test_session"
TIMEOUT_SECONDS = 90  # Alfred can be slow on complex tasks
REQUEST_DELAY = 3  # Seconds between requests to avoid Groq rate limits

# Force UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Data Types ──────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    category: str
    question: str
    response: str
    thinking: list
    passed: bool
    score: float  # 0.0 to 1.0
    notes: str
    response_time: float = 0.0
    tools_used: int = 0


@dataclass
class TestSuite:
    results: List[TestResult] = field(default_factory=list)

    @property
    def total(self):
        return len(self.results)

    @property
    def passed(self):
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self):
        return self.total - self.passed

    @property
    def score(self):
        if not self.results:
            return 0.0
        return sum(r.score for r in self.results) / len(self.results)

    @property
    def category_scores(self):
        cats = {}
        for r in self.results:
            if r.category not in cats:
                cats[r.category] = {"passed": 0, "total": 0, "score": 0.0}
            cats[r.category]["total"] += 1
            cats[r.category]["score"] += r.score
            if r.passed:
                cats[r.category]["passed"] += 1
        for cat in cats:
            cats[cat]["score"] /= cats[cat]["total"]
        return cats


# ── Helpers ─────────────────────────────────────────────────────────────────

def send_message(message: str) -> dict:
    """Send a message to Alfred and return the raw response."""
    try:
        r = requests.post(
            COMMAND_ENDPOINT,
            json={"message": message, "session_id": SESSION_ID},
            timeout=TIMEOUT_SECONDS,
        )
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"error": "CONNECTION_REFUSED"}
    except requests.exceptions.Timeout:
        return {"error": "TIMEOUT"}
    except Exception as e:
        return {"error": str(e)}


def check_server_health() -> dict:
    """Check if Alfred server is running and healthy."""
    try:
        r = requests.get(HEALTH_ENDPOINT, timeout=10)
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"status": "DOWN"}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


def count_tools(thinking: list) -> int:
    """Count how many tools Alfred actually called from thinking trace."""
    count = 0
    for line in thinking:
        # Format: "Iteration N: tool_name description" or "Running tool: ..."
        if re.match(r"Iteration \d+:", line):
            count += 1
        elif "Running tool" in line or "tool:" in line.lower():
            count += 1
    return count


def response_contains(response: str, keywords: list, case_sensitive=False) -> bool:
    """Check if response contains any of the keywords."""
    text = response if case_sensitive else response.lower()
    for kw in keywords:
        target = kw if case_sensitive else kw.lower()
        if target in text:
            return True
    return False


def response_contains_any_pattern(response: str, patterns: list) -> bool:
    """Check if response matches any regex pattern."""
    for pat in patterns:
        if re.search(pat, response, re.IGNORECASE):
            return True
    return False


# ── Test Definitions ────────────────────────────────────────────────────────
# Each test: (name, category, question, evaluator_fn)
# evaluator_fn(response, thinking) -> (passed: bool, score: float, notes: str)

TESTS = [
    # ── Memory & Recall (1-3) ───────────────────────────────────────────────
    (
        "T01: Primary Goals",
        "Memory & Recall",
        "What are my primary goals right now?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"mit", r"99th percentile", r"cbse", r"30k", r"business",
                r"thirty.*thousand", r"dollar.*30k",
            ]),
            1.0 if response_contains_any_pattern(r, ["mit", "cbse", "30k"]) else 0.3,
            "Should mention MIT admission, 99th percentile CBSE, or $30k business"
        ),
    ),
    (
        "T02: Favorite Food",
        "Memory & Recall",
        "What's my favorite food?",
        lambda r, t: (
            response_contains(r, ["chicken", "biryani", "biriyani"]),
            1.0 if response_contains(r, ["biryani", "biriyani"]) else 0.5 if response_contains(r, ["chicken"]) else 0.0,
            "Should respond: Chicken biryani"
        ),
    ),
    (
        "T03: Earlier Conversation",
        "Memory & Recall",
        "What did we talk about earlier today?",
        lambda r, t: (
            len(r) > 20 and not response_contains(r, ["i don't know", "i don't have", "no context", "no memory"]),
            0.8 if len(r) > 30 else 0.4,
            "Should reference session context or recent conversations"
        ),
    ),

    # ── Calendar & Email (4-7) ───────────────────────────────────────────────
    (
        "T04: Calendar Check",
        "Calendar & Email",
        "Do I have any events on my calendar today?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"event", r"calendar", r"no event", r"nothing.*schedule",
                r"today.*schedule", r"free", r"busy", r"no.*meeting",
            ]) and count_tools(t) > 0,
            1.0 if count_tools(t) > 0 else 0.2,
            "Must actually call calendar tool. Not just 'I'll check'."
        ),
    ),
    (
        "T05: Add Study Block",
        "Calendar & Email",
        "Add a study block for Physics tomorrow at 4pm.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"creat", r"added", r"study block", r"physics", r"4.*pm",
                r"tomorrow", r"event.*creat", r"done", r"confirm",
            ]) and count_tools(t) > 0,
            1.0 if response_contains(r, ["physics"]) and count_tools(t) > 0 else 0.3,
            "Should create event and confirm. Verify with read-back."
        ),
    ),
    (
        "T06: Check Unread Emails",
        "Calendar & Email",
        "Check my unread emails.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"email", r"unread", r"inbox", r"no.*email", r"0.*email",
                r"message", r"mail",
            ]) and count_tools(t) > 0,
            1.0 if count_tools(t) > 0 else 0.2,
            "Must call email tool. Even 0 emails should say so."
        ),
    ),
    (
        "T07: Send Test Email",
        "Calendar & Email",
        "Send a test email to myself with subject 'Phase 1 Test' and body 'Alfred is working.'",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"sent", r"email.*sent", r"delivered", r"confirm", r"done",
            ]) and count_tools(t) > 0,
            1.0 if response_contains(r, ["sent"]) and count_tools(t) > 0 else 0.3,
            "Should send email and verify delivery."
        ),
    ),

    # ── Verification Loop (8-9) ─────────────────────────────────────────────
    (
        "T08: Delete and Verify",
        "Verification Loop",
        "Delete the Physics study block you just created.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"delet", r"remov", r"cleared", r"done", r"removed",
            ]),
            1.0 if response_contains_any_pattern(r, [r"delet", r"remov"]) else 0.3,
            "Should delete AND verify it's gone. Check if it read calendar after."
        ),
    ),
    (
        "T09: Verify Reminder",
        "Verification Loop",
        "I think the reminder you set earlier isn't there. Can you verify?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"remind", r"verif", r"check", r"list", r"found", r"not found",
                r"doesn't exist", r"is there",
            ]),
            0.8,
            "Should list reminders and confirm status."
        ),
    ),

    # ── Proactivity / Cognitive Heartbeat (10-12) ────────────────────────────
    (
        "T10: Monday Test Sunday Plan",
        "Proactivity",
        "I have a test every Monday. What should my Sunday look like?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"study", r"review", r"prep", r"schedule", r"sunday",
                r"plan", r"revise", r"rest",
            ]) and len(r) > 50,
            1.0 if response_contains_any_pattern(r, ["study", "review", "prep"]) else 0.3,
            "Should suggest study schedule. If says 'I don't know', cognitive cycle is broken."
        ),
    ),
    (
        "T11: Stressed About Exams",
        "Proactivity",
        "I'm feeling really stressed about my exams.",
        lambda r, t: (
            len(r) > 50 and not response_contains_any_pattern(r, [
                r"you'll be fine", r"don't worry", r"everything.*ok",
                r"just relax", r"you got this",
            ]),
            1.0 if response_contains_any_pattern(r, ["mit", "cbse", "goal", "plan", "strategy"]) else 0.4,
            "Logic-based motivation referencing goals. No empty platitudes."
        ),
    ),
    (
        "T12: Wasted Hour on Instagram",
        "Proactivity",
        "I just wasted an hour on Instagram.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"wast", r"rethink", r"reset", r"goal", r"time.*manag",
                r"productiv", r"focus", r"move.*forward",
            ]) and not response_contains_any_pattern(r, [
                r"that's ok", r"no problem", r"don't worry", r"it's fine",
            ]),
            1.0 if response_contains_any_pattern(r, ["goal", "reset", "focus"]) else 0.3,
            "Should call out the behavior with logic, reference goals, suggest reset."
        ),
    ),

    # ── Self-Correction (13-14) ─────────────────────────────────────────────
    (
        "T13: Correction Mid-Query",
        "Self-Correction",
        "What's the weather in Tokyo? Actually wait, I meant Delhi.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"delhi", r"weather",
            ]),
            1.0 if response_contains(r, ["delhi"]) else 0.3,
            "Should handle correction gracefully. Delhi weather, not Tokyo."
        ),
    ),
    (
        "T14: Invalid Time Handling",
        "Self-Correction",
        "Add an event to my calendar at an invalid time like 25:00.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"invalid", r"error", r"can't", r"cannot", r"not valid",
                r"wrong", r"25:00.*invalid", r"invalid.*time",
            ]),
            1.0 if response_contains_any_pattern(r, ["invalid", "error", "not valid"]) else 0.2,
            "Should catch the error and report invalid time, not fail silently."
        ),
    ),

    # ── Memory Persistence (15-16) ──────────────────────────────────────────
    (
        "T15: Remember Friend Birthday",
        "Memory Persistence",
        "Remember that my friend Alex's birthday is July 15th.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"remember", r"saved", r"noted", r"alex.*birthday",
                r"july.*15", r"stored", r"got it",
            ]),
            1.0 if response_contains_any_pattern(r, ["saved", "noted", "remember", "stored"]) else 0.4,
            "Should save to T4 and confirm."
        ),
    ),
    (
        "T16: Recall Study Habits",
        "Memory Persistence",
        "What have I told you about my study habits?",
        lambda r, t: (
            len(r) > 30 and not response_contains_any_pattern(r, [
                r"don't know", r"no information", r"nothing.*told",
                r"no record", r"no data",
            ]),
            0.8 if len(r) > 50 else 0.4,
            "Should search T3/T5 and return relevant past conversations."
        ),
    ),

    # ── Edge Cases (17-20) ───────────────────────────────────────────────────
    (
        "T17: Casual Greeting Speed",
        "Edge Cases",
        "yo what's good?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"good", r"hey", r"yo", r"sup", r"what's up", r"hello",
                r"how.*you", r"going",
            ]) and count_tools(t) == 0,
            1.0 if count_tools(t) == 0 else 0.3,
            "Should respond in <5s with casual greeting. No tools should be called."
        ),
    ),
    (
        "T18: Typo Tolerance",
        "Edge Cases",
        "Wat is my calender tomorow?",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"calendar", r"tomorrow", r"event", r"schedule",
                r"no event", r"nothing",
            ]),
            1.0 if count_tools(t) > 0 else 0.4,
            "Should understand typos and show tomorrow's calendar."
        ),
    ),
    (
        "T19: Reschedule Help",
        "Edge Cases",
        "I need to reschedule but I don't know what I have tomorrow. Help.",
        lambda r, t: (
            response_contains_any_pattern(r, [
                r"tomorrow", r"calendar", r"event", r"schedule",
                r"here.*what", r"let me",
            ]),
            1.0 if count_tools(t) > 0 else 0.3,
            "Should show tomorrow's calendar first, then ask which event."
        ),
    ),
    (
        "T20: Self-Awareness",
        "Edge Cases",
        "Tell me honestly — what's one thing you failed at today and how did you recover?",
        lambda r, t: (
            len(r) > 30,
            0.9 if response_contains_any_pattern(r, [
                "error", "fail", "mistake", "recover", "retry",
                "learn", "improve", "nothing.*failed",
            ]) else 0.5,
            "Should reference actual errors or honestly say nothing failed."
        ),
    ),
]


# ── Runner ──────────────────────────────────────────────────────────────────

def run_all_tests():
    """Run the full Phase 1 cockpit test suite."""
    suite = TestSuite()

    # ── Pre-flight checks ───────────────────────────────────────────────
    print("=" * 70)
    print("  ALFRED PHASE 1 COCKPIT INTEGRATION TEST")
    print("  Testing 20 real-world questions against live server")
    print("=" * 70)
    print()

    print("[1/3] Checking server health...")
    health = check_server_health()
    if health.get("status") == "DOWN" or health.get("status") == "ERROR":
        print(f"  ❌ Server is not running at {BASE_URL}")
        print(f"  Start it with: python project-alfred/brain_api/server.py")
        print(f"  Error: {health.get('error', 'Connection refused')}")
        sys.exit(1)

    print(f"  ✅ Server is up — uptime {health.get('uptime_seconds', '?')}s")
    memory_tiers = health.get("memory_tiers", {})
    if memory_tiers:
        print(f"  Memory tiers: {json.dumps(memory_tiers)}")
    print()

    # Check GWS auth status
    print("[2/3] Checking GWS authentication...")
    try:
        auth_r = requests.get(f"{BASE_URL}/api/auth/gws", timeout=10)
        auth_data = auth_r.json()
        if auth_data.get("authenticated"):
            print(f"  ✅ GWS authenticated (scopes: {len(auth_data.get('scopes', []))})")
        else:
            print(f"  ⚠️  GWS not authenticated — calendar/email tests will likely fail")
            print(f"  Auth at: POST {BASE_URL}/api/auth/gws/login")
    except Exception:
        print(f"  ⚠️  Could not check GWS auth status")
    print()

    # ── Run tests ───────────────────────────────────────────────────────
    print(f"[3/3] Running {len(TESTS)} tests...")
    print("-" * 70)

    current_category = ""
    for i, (name, category, question, evaluator) in enumerate(TESTS):
        # Category header
        if category != current_category:
            current_category = category
            print(f"\n  ── {category} {'─' * (50 - len(category))}")

        # Send question
        if i > 0:
            time.sleep(REQUEST_DELAY)  # Avoid Groq rate limits
        sys.stdout.write(f"  {name}... ")
        sys.stdout.flush()

        start_time = time.time()
        raw = send_message(question)
        elapsed = time.time() - start_time

        # Handle connection errors
        if "error" in raw:
            result = TestResult(
                name=name, category=category, question=question,
                response="", thinking=[], passed=False, score=0.0,
                notes=f"Server error: {raw['error']}",
                response_time=elapsed, tools_used=0,
            )
            print(f"❌ ERROR ({elapsed:.1f}s) — {raw['error']}")
            suite.results.append(result)
            continue

        response = raw.get("response", "")
        thinking = raw.get("thinking", [])
        tools = count_tools(thinking)

        # Evaluate
        try:
            passed, score, notes = evaluator(response, thinking)
        except Exception as e:
            passed, score, notes = False, 0.0, f"Evaluator error: {e}"

        result = TestResult(
            name=name, category=category, question=question,
            response=response, thinking=thinking,
            passed=passed, score=score, notes=notes,
            response_time=elapsed, tools_used=tools,
        )
        suite.results.append(result)

        # Output
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} ({elapsed:.1f}s, {tools} tools, score={score:.1f})")
        if not passed:
            # Show truncated response for failed tests
            safe = response[:120].encode("ascii", "replace").decode("ascii")
            print(f"           Response: {safe}")
            print(f"           Expected: {notes}")

    # ── Report ──────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)
    print()

    # Overall
    grade = (
        "A" if suite.score >= 0.9 else
        "B" if suite.score >= 0.75 else
        "C" if suite.score >= 0.6 else
        "D" if suite.score >= 0.4 else
        "F"
    )
    print(f"  Overall: {suite.passed}/{suite.total} passed | Score: {suite.score:.0%} | Grade: {grade}")
    print()

    # By category
    print("  By Category:")
    for cat, data in suite.category_scores.items():
        bar_len = int(data["score"] * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        status = "✅" if data["score"] >= 0.7 else "⚠️" if data["score"] >= 0.4 else "❌"
        print(f"  {status} {cat:<22} {bar} {data['passed']}/{data['total']} ({data['score']:.0%})")

    print()

    # Failed tests detail
    failed = [r for r in suite.results if not r.passed]
    if failed:
        print("  Failed Tests:")
        for r in failed:
            print(f"    ❌ {r.name}")
            print(f"       Q: {r.question[:60]}")
            print(f"       Why: {r.notes}")
            safe_resp = r.response[:100].encode("ascii", "replace").decode("ascii")
            print(f"       Got: {safe_resp}")
            print()
    else:
        print("  🎉 All tests passed!")
        print()

    # Phase 1 verdict
    print("-" * 70)
    print("  PHASE 1 VERDICT:")
    if suite.score >= 0.8:
        print("  ✅ Phase 1 (Sovereign Core) is working in practice.")
    elif suite.score >= 0.6:
        print("  ⚠️  Phase 1 partially working. Some features need attention.")
    else:
        print("  ❌ Phase 1 is NOT working in practice. Core features failing.")
        print("  Review the failed tests above and fix the underlying issues.")

    print()
    print(f"  Full results saved to: phase1_results.json")
    print("=" * 70)

    # Save detailed results
    results_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overall_score": suite.score,
        "grade": grade,
        "passed": suite.passed,
        "total": suite.total,
        "categories": suite.category_scores,
        "tests": [
            {
                "name": r.name,
                "category": r.category,
                "question": r.question,
                "passed": r.passed,
                "score": r.score,
                "notes": r.notes,
                "response_time": round(r.response_time, 2),
                "tools_used": r.tools_used,
                "response_preview": r.response[:300],
            }
            for r in suite.results
        ],
    }
    with open("phase1_results.json", "w", encoding="utf-8") as f:
        json.dump(results_data, f, indent=2, ensure_ascii=False)

    return suite.score


if __name__ == "__main__":
    score = run_all_tests()
    sys.exit(0 if score >= 0.7 else 1)
