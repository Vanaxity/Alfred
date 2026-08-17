"""
Goal Inference Module

Expands user input into canonical goal statements for better skill matching.
Example: "Calendar" → "Retrieve and display today's calendar events"
"""

import os
from typing import Optional
from dataclasses import dataclass

# Check for API keys
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


@dataclass
class GoalInference:
    """Result of goal inference."""

    original: str
    expanded: str
    confidence: float
    suggested_tools: list


class GoalExpander:
    """
    Expands short user inputs into canonical goal statements.

    Uses a simple prompt to the LLM to expand ambiguous inputs.
    """

    SYSTEM_PROMPT = """Expand user input into a clear goal statement. Rules: under 20 words, be specific, preserve all details. Reply ONLY with the expanded goal."""

    def __init__(self):
        self.api_key = GROQ_API_KEY
        self.enabled = True  # LLM expansion enabled with async wrapper

    async def expand(self, user_input: str) -> GoalInference:
        """
        Expand user input into canonical goal.

        Args:
            user_input: The user's raw input

        Returns:
            GoalInference with original, expanded, confidence, and suggested tools
        """
        if not self.enabled:
            # Fallback to simple keyword expansion
            return self._simple_expand(user_input)

        try:
            return await self._llm_expand(user_input)
        except Exception as e:
            print(f"Goal inference error: {e}")
            return self._simple_expand(user_input)

    async def _llm_expand(self, user_input: str) -> GoalInference:
        """Use LLM for goal expansion."""

        response = await self._call_llm(user_input)
        if not response:
            return self._simple_expand(user_input)
        expanded = response.strip()

        # Suggest tools based on expanded goal
        tools = self._suggest_tools(expanded)

        return GoalInference(
            original=user_input,
            expanded=expanded,
            confidence=0.9,
            suggested_tools=tools,
        )

    async def _call_llm(self, user_input: str) -> str:
        """Call Groq for goal expansion (free llama-3.1-8b-instant)."""
        import asyncio

        if not self.api_key:
            return ""

        def _do_request():
            from groq import Groq
            client = Groq(api_key=self.api_key)
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_input},
                ],
                temperature=0.3,
                max_tokens=100,
            )
            return response.choices[0].message.content or ""

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_do_request), timeout=8.0
            )
        except Exception:
            return ""

    def _simple_expand(self, user_input: str) -> GoalInference:
        """Simple keyword-based expansion (fallback)."""
        tools = self._suggest_tools(user_input)

        # Only expand very short inputs; leave well-formed tasks as-is
        word_count = len(user_input.split())
        if word_count >= 4:
            return GoalInference(
                original=user_input,
                expanded=user_input,
                confidence=0.6,
                suggested_tools=tools,
            )

        # Simple expansions for short inputs
        expansions = {
            "calendar": "Retrieve and display calendar events",
            "email": "Check email inbox and display messages",
            "weather": "Get current weather information",
            "search": "Search the web for information",
            "code": "Assist with coding tasks",
            "hello": "Greet the user and engage in conversation",
            "hi": "Greet the user and engage in conversation",
            "tasks": "Show and manage task list",
            "weather in": "Get weather for specific location",
            "time": "Get the current time and date",
            "remind me": "Set a reminder or show existing reminders",
            "remember": "Save or recall information from memory",
            "calculate": "Perform a mathematical calculation",
            "math": "Perform a mathematical calculation",
            "file": "Read, write, or manage files on disk",
            "screenshot": "Take a screenshot of the screen",
            "open": "Open an application or file",
            "joke": "Tell a joke or funny story",
            "help": "Show available commands and capabilities",
            "motivate": "Provide motivation and encouragement",
            "study": "Help with study planning and focus",
            "schedule": "Manage calendar schedule and events",
            "read": "Read content from a file or document",
        }

        expanded = user_input
        user_lower = user_input.lower().strip()
        for keyword, expansion in expansions.items():
            if user_lower == keyword or user_lower.startswith(keyword + " "):
                expanded = expansion
                break

        return GoalInference(
            original=user_input,
            expanded=expanded,
            confidence=0.6,
            suggested_tools=tools,
        )

    def _suggest_tools(self, expanded_goal: str) -> list:
        """Suggest tools based on expanded goal."""
        tools = []
        goal_lower = expanded_goal.lower()

        if "calendar" in goal_lower or "event" in goal_lower:
            tools.append("calendar")
        if "email" in goal_lower or "gmail" in goal_lower:
            tools.append("email")
        if "search" in goal_lower or "web" in goal_lower:
            tools.append("web_search")
        if "weather" in goal_lower:
            tools.append("weather")
        if "code" in goal_lower or "programming" in goal_lower:
            tools.append("run_code")
        if "file" in goal_lower or "folder" in goal_lower:
            tools.append("read_file")
        if "time" in goal_lower:
            tools.append("time")
        if "remind" in goal_lower:
            tools.append("set_reminder")
        if "calculate" in goal_lower or "math" in goal_lower:
            tools.append("calculator")
        if "screenshot" in goal_lower:
            tools.append("screenshot")
        if "memory" in goal_lower or "remember" in goal_lower:
            tools.append("memory_search")

        if not tools:
            tools.append("chat")

        return tools


# Singleton instance
_expander: Optional[GoalExpander] = None


def get_goal_expander() -> GoalExpander:
    """Get singleton goal expander instance."""
    global _expander
    if _expander is None:
        _expander = GoalExpander()
    return _expander
