"""
Alfred Loop Validation Suite v1.0

Tests the core execution loop behavior with mocked external dependencies.
Each test validates tool call sequences, error recovery, and final responses.

Usage:
    python -m brain.loop_tests          # Run all tests
    python -m brain.loop_tests --test 3 # Run specific test
    python -m brain.loop_tests --verbose # Show full output
"""

import sys
import asyncio
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from brain.alfred import Alfred  # noqa: E402


# ============ MOCK TOOL REGISTRY ============

class MockToolRegistry:
    """Deterministic mock tool responses for testing."""

    def __init__(self):
        self._call_history = []
        self._mock_responses = {}
        self._mock_errors = {}
        self._file_system = {}

    def record_call(self, tool: str, params: Dict):
        self._call_history.append({"tool": tool, "params": params})

    def clear(self):
        self._call_history.clear()
        self._mock_responses.clear()
        self._mock_errors.clear()
        self._file_system.clear()

    @property
    def call_history(self) -> List[Dict]:
        return list(self._call_history)

    def set_mock(self, tool: str, response: str):
        self._mock_responses[tool] = response

    def set_error(self, tool: str, error: str, call_count: int = 1):
        if tool not in self._mock_errors:
            self._mock_errors[tool] = []
        self._mock_errors[tool].append({"error": error, "remaining": call_count})

    def set_file(self, path: str, content: str):
        self._file_system[path] = content

    def mock_execute(self, tool: str, params: Dict) -> Dict:
        self.record_call(tool, params)

        # Check for staged errors
        if tool in self._mock_errors:
            for err_info in self._mock_errors[tool]:
                if err_info["remaining"] > 0:
                    err_info["remaining"] -= 1
                    return {"error": err_info["error"]}

        # Return mock response
        if tool in self._mock_responses:
            return {"output": self._mock_responses[tool]}

        defaults = {
            "calendar": "Upcoming events:\n  - 9:00 AM Standup\n  - 2:00 PM Client Call",
            "email": "Recent emails (4 total):\n  - Invoice from AWS\n  - Team update\n  - Newsletter\n  - Security alert",
            "web_search": "Results:\n1. https://example.com/ai-news - Top AI News: New regulations proposed for AI systems\n2. https://example.com/ai-regulation - AI Regulation Update 2026",
            "web_fetch": "Title: AI Regulation Update\n\nThe European Union has proposed new regulations for AI systems, focusing on transparency and accountability. The rules require companies to disclose when AI is used in decision-making processes. Industry leaders have responded with mixed reactions.",
            "time": "Thursday, May 21, 2026 at 03:00 PM (IST)",
            "weather": "Mumbai: Partly cloudy 32°C, 65% humidity, 12 km/h wind",
            "read_file": "",
            "shell": "",
            "memory_search": "",
            "memory_save": "Saved to T4 user profile.",
            "remember": "Saved to T4 user profile.",
            "chat": "I understand.",
            "calculator": "4",
            "open_app": "Application opened.",
            "list_reminders": "No reminders found.",
            "delete_reminder": "Reminder deleted.",
            "set_reminder": "Reminder set.",
            "gws": "No files found.",
            "run_code": "",
            "write_file": "File written.",
            "list_directory": "file1.txt\nfile2.py\ndir1/",
            "glob": "file1.txt\nfile2.py",
            "screenshot": "Screenshot saved.",
        }
        return {"output": defaults.get(tool, f"Mock output for {tool}")}


# ============ TEST HARNESS ============

@dataclass
class TestResult:
    test_id: int
    name: str
    passed: bool
    tool_calls: List[Dict] = field(default_factory=list)
    response: str = ""
    error: str = ""
    details: str = ""


class AlfredTestHarness:
    """Test harness that wraps Alfred with mocked dependencies."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.mocks = MockToolRegistry()
        self.results: List[TestResult] = []
        self._llm_plan_mock = None
        self._llm_action_mock = None

    def _create_alfred(self) -> Alfred:
        alfred = Alfred()
        async def mock_execute_wrapper(tool: str, params: Dict, context: Dict = None) -> Dict:
            return self.mocks.mock_execute(tool, params)

        alfred._execute_tool = mock_execute_wrapper
        alfred._get_groq_client = lambda: None

        # Mock LLM — returns plans for generation, raw output for formatting
        async def mock_call_llm(system_prompt, user_content, model=None, **kwargs):
            # Formatting call: has "Raw tool output" in user_content
            if "Raw tool output" in (user_content or "") or "Answer the user" in (system_prompt or ""):
                # Extract raw output from user_content and return as-is
                if "Raw tool output" in (user_content or ""):
                    parts = (user_content or "").split("Raw tool output:")
                    if len(parts) > 1:
                        return parts[-1].strip()
                return "Task completed."
            if self._llm_plan_mock:
                return self._llm_plan_mock
            # Default: return a single-step plan based on user task content
            task = user_content.lower() if user_content else ""
            if "calendar" in task or "schedule" in task or "meeting" in task:
                return '[{"tool": "calendar", "description": "Check calendar", "params": {"action": "agenda"}}]'
            if "email" in task or "gmail" in task or "inbox" in task or "mail" in task:
                return '[{"tool": "email", "description": "Check email", "params": {"action": "triage"}}]'
            if "web_search" in task or ("search" in task and "memory" not in task):
                return '[{"tool": "web_search", "description": "Search", "params": {"query": "test"}}]'
            if "memory" in task or "remember" in task or "preference" in task:
                return '[{"tool": "memory_search", "description": "Search memory", "params": {"query": "test"}}]'
            if "shell" in task or "list" in task or "files" in task or "count" in task:
                return '[{"tool": "shell", "description": "Run command", "params": {"command": "ls"}}]'
            if "read" in task and "file" in task:
                return '[{"tool": "read_file", "description": "Read file", "params": {"path": "test"}}]'
            if "tweet" in task or "twitter" in task or "post" in task:
                return '[{"tool": "chat", "description": "Handle", "params": {"message": "I cannot post tweets."}}]'
            return '[{"tool": "chat", "description": "Handle", "params": {"message": "done"}}]'

        alfred._call_llm = mock_call_llm
        return alfred

    def set_llm_plan(self, plan_json: str):
        """Set the LLM plan response for the next test."""
        self._llm_plan_mock = plan_json

    def _assert_tool_sequence(self, expected_tools: List[str], actual_calls: List[Dict]) -> bool:
        actual_tool_names = [c["tool"] for c in actual_calls]
        for i, expected in enumerate(expected_tools):
            if i >= len(actual_tool_names):
                return False
            if actual_tool_names[i] != expected:
                return False
        return True

    def _assert_response_contains(self, response: str, required_phrases: List[str]) -> bool:
        response_lower = response.lower()
        return all(phrase.lower() in response_lower for phrase in required_phrases)

    def _assert_no_tool_calls(self, actual_calls: List[Dict]) -> bool:
        return len(actual_calls) == 0

    async def run_test(self, test_id: int, name: str, input_text: str,
                       setup_fn: Optional[Callable] = None,
                       expected_tools: Optional[List[str]] = None,
                       required_phrases: Optional[List[str]] = None,
                       should_not_contain: Optional[List[str]] = None,
                       no_tool_calls: bool = False,
                       llm_plan: Optional[str] = None) -> TestResult:
        result = TestResult(test_id=test_id, name=name, passed=False)

        try:
            self.mocks.clear()
            self._llm_plan_mock = llm_plan
            if setup_fn:
                setup_fn(self.mocks)

            alfred = self._create_alfred()
            response_data = await alfred.execute(input_text)
            response_text = response_data.get("response", "")
            result.response = response_text
            result.tool_calls = self.mocks.call_history

            if no_tool_calls:
                if not self._assert_no_tool_calls(result.tool_calls):
                    result.error = f"Expected no tool calls, but got: {[c['tool'] for c in result.tool_calls]}"
                    return result

            if expected_tools:
                if not self._assert_tool_sequence(expected_tools, result.tool_calls):
                    actual = [c["tool"] for c in result.tool_calls]
                    result.error = f"Expected tools {expected_tools}, got {actual}"
                    return result

            if required_phrases:
                if not self._assert_response_contains(response_text, required_phrases):
                    result.error = f"Response missing required phrases. Response: {response_text[:200]}"
                    return result

            if should_not_contain:
                for phrase in should_not_contain:
                    if phrase.lower() in response_text.lower():
                        result.error = f"Response should not contain '{phrase}'"
                        return result

            result.passed = True

        except Exception as e:
            result.error = str(e)
            if self.verbose:
                import traceback
                result.details = traceback.format_exc()

        return result

    async def run_all(self):
        print("=" * 60)
        print("ALFRED LOOP VALIDATION SUITE v1.0")
        print("=" * 60)
        print()

        tests = [
            self.test_1_calendar_fetch,
            self.test_2_email_triage,
            self.test_3_multi_step_search,
            self.test_4_missing_file_recovery,
            self.test_5_bounced_email_memory,
            self.test_6_hallucinated_tool,
            self.test_7_preference_update,
            self.test_8_context_stress,
            self.test_9_skill_patching,
            self.test_10_ambiguity,
        ]

        for i, test_fn in enumerate(tests, 1):
            print(f"Running Test {i}: {test_fn.__doc__ or test_fn.__name__}...")
            result = await test_fn()
            self.results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {result.name}")
            if result.error:
                print(f"  Error: {result.error[:150]}")
            if self.verbose and result.response:
                print(f"  Response: {result.response[:200]}")
            print()

        print("=" * 60)
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        print(f"RESULTS: {passed}/{total} passed")
        print()
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  {status} Test {r.test_id}: {r.name}")
        print("=" * 60)

        return passed == total

    # ============ TEST CASES ============

    async def test_1_calendar_fetch(self) -> TestResult:
        """Simple Calendar Fetch"""
        return await self.run_test(
            test_id=1,
            name="Simple Calendar Fetch",
            input_text="What's on my calendar today?",
            setup_fn=lambda m: m.set_mock("calendar", "Upcoming events:\n  - 9:00 AM Standup\n  - 2:00 PM Client Call"),
            expected_tools=["calendar"],
            required_phrases=["standup", "client call"],
        )

    async def test_2_email_triage(self) -> TestResult:
        """Email Triage & Count"""
        return await self.run_test(
            test_id=2,
            name="Email Triage & Count",
            input_text="How many unread emails do I have?",
            setup_fn=lambda m: m.set_mock("email", "Recent emails (4 total):\n  - Invoice from AWS\n  - Team update\n  - Newsletter\n  - Security alert"),
            expected_tools=["email"],
            required_phrases=["4"],
        )

    async def test_3_multi_step_search(self) -> TestResult:
        """Multi-Step: Search, Read, Summarize"""
        return await self.run_test(
            test_id=3,
            name="Multi-Step Search→Read→Summarize",
            input_text="Find the top news about AI today, read the first article, and give me a 2-sentence summary.",
            setup_fn=lambda m: (
                m.set_mock("web_search", "Results:\n1. https://example.com/ai-news - Top AI News: New regulations proposed for AI systems\n2. https://example.com/ai-regulation - AI Regulation Update 2026"),
                m.set_mock("web_fetch", "Title: AI Regulation Update\n\nThe European Union has proposed new regulations for AI systems, focusing on transparency and accountability. The rules require companies to disclose when AI is used in decision-making processes."),
            ),
            llm_plan='[{"tool": "web_search", "description": "Search AI news", "params": {"query": "top AI news today"}}, {"tool": "web_fetch", "description": "Read article", "params": {"url": "https://example.com/ai-news"}}]',
            expected_tools=["web_search", "web_fetch"],
            required_phrases=["regulation", "ai"],
        )

    async def test_4_missing_file_recovery(self) -> TestResult:
        """Missing File Recovery"""
        return await self.run_test(
            test_id=4,
            name="Missing File Recovery",
            input_text="Email my monthly report using the template at ~/.alfred/templates/report.html.",
            setup_fn=lambda m: (
                m.set_error("read_file", "File not found: ~/.alfred/templates/report.html", call_count=1),
                m.set_file("~/.alfred/templates/archived/report.html", "<html>Monthly Report Template</html>"),
                m.set_mock("email", "Email sent to team@example.com."),
            ),
            required_phrases=["email sent"],
            should_not_contain=["file not found"],
        )

    async def test_5_bounced_email_memory(self) -> TestResult:
        """Bounced Email & Memory Retrieval"""
        return await self.run_test(
            test_id=5,
            name="Bounced Email + Memory Recovery",
            input_text="Send a test email to alex@example.com with subject 'Test'.",
            setup_fn=lambda m: (
                m.set_error("email", "Delivery failed: alex@example.com bounced", call_count=1),
                m.set_mock("memory_search", "[T5] Alex email: alex.johnson@example.com"),
                m.set_mock("email", "Email sent successfully to alex.johnson@example.com."),
            ),
            required_phrases=["alex.johnson@example.com"],
        )

    async def test_6_hallucinated_tool(self) -> TestResult:
        """Hallucinated Tool Refusal"""
        return await self.run_test(
            test_id=6,
            name="Hallucinated Tool Refusal",
            input_text="Post a tweet saying 'Hello world' using the Twitter API tool.",
            setup_fn=lambda m: m.set_mock("chat", "I don't have a tool to post tweets. I can search X or help you draft it. Would you like me to open Twitter in the browser instead?"),
            expected_tools=["chat"],
            required_phrases=["don't have", "tweet"],
        )

    async def test_7_preference_update(self) -> TestResult:
        """Contradictory Preference Update"""
        return await self.run_test(
            test_id=7,
            name="Contradictory Preference Update",
            input_text="What's my code display preference?",
            setup_fn=lambda m: m.set_mock("memory_search", "[T4] code_display: light mode"),
            expected_tools=["memory_search"],
            required_phrases=["light mode"],
            should_not_contain=["dark mode"],
        )

    async def test_8_context_stress(self) -> TestResult:
        """Context Window Stress (Large Tool Output)"""
        large_output = "\n".join([f"file_{i}" for i in range(3000)])
        return await self.run_test(
            test_id=8,
            name="Context Window Stress",
            input_text="List all files in /usr/bin and tell me how many there are.",
            setup_fn=lambda m: m.set_mock("shell", large_output),
            expected_tools=["shell"],
            required_phrases=["3000", "lines"],
        )

    async def test_9_skill_patching(self) -> TestResult:
        """Broken Skill Patching"""
        return await self.run_test(
            test_id=9,
            name="Broken Skill Patching",
            input_text="Use the 'send-daily-report' skill to email the report.",
            setup_fn=lambda m: (
                m.set_error("email", "Error: unrecognized parameter '--priority'", call_count=1),
                m.set_mock("email", "Email sent. Skill updated to remove invalid flag."),
            ),
            required_phrases=["email sent", "skill"],
        )

    async def test_10_ambiguity(self) -> TestResult:
        """Ambiguous Request Disambiguation"""
        return await self.run_test(
            test_id=10,
            name="Ambiguous Request Disambiguation",
            input_text="Move my morning meeting to 10 AM.",
            setup_fn=lambda m: m.set_mock("calendar", "AMBIGUITY: Found 2 events at similar times: Standup, Client Call. Which one should I move?"),
            expected_tools=["calendar"],
            required_phrases=["2", "standup", "client call", "which"],
        )


# ============ MAIN ============

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Alfred Loop Validation Suite")
    parser.add_argument("--test", type=int, help="Run specific test number (1-10)")
    parser.add_argument("--verbose", action="store_true", help="Show full output")
    args = parser.parse_args()

    harness = AlfredTestHarness(verbose=args.verbose)

    if args.test:
        test_fns = {
            1: harness.test_1_calendar_fetch,
            2: harness.test_2_email_triage,
            3: harness.test_3_multi_step_search,
            4: harness.test_4_missing_file_recovery,
            5: harness.test_5_bounced_email_memory,
            6: harness.test_6_hallucinated_tool,
            7: harness.test_7_preference_update,
            8: harness.test_8_context_stress,
            9: harness.test_9_skill_patching,
            10: harness.test_10_ambiguity,
        }
        if args.test not in test_fns:
            print(f"Invalid test number: {args.test}")
            return
        print(f"Running Test {args.test}...")
        result = await test_fns[args.test]()
        harness.results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}")
        if result.error:
            print(f"Error: {result.error}")
        if result.response:
            print(f"Response: {result.response[:300]}")
    else:
        await harness.run_all()


if __name__ == "__main__":
    asyncio.run(main())
