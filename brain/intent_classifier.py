"""
Intent Classifier - Routes user commands to the correct tool/category.
Uses Groq llama-3.1-8b-instant for fast (~0.5s), accurate classification.
"""

import os
import re
import logging
import requests

logger = logging.getLogger(__name__)

CATEGORIES = [
    "math",
    "calendar",
    "reminder",
    "memory",
    "email",
    "web_search",
    "file",
    "shell",
    "chat",
    "open_app",
    "screenshot",
    "time",
    "weather",
]

# Keywords for fast local fallback (no LLM call needed)
KEYWORD_MAP = {
    "math": [
        "calculate", "what is", "compute", "times", "plus", "minus",
        "divide", "multiply", "sum", "average", "convert", "how much is",
        "evaluate", "solve", "equation",
    ],
    "calendar": [
        "calendar", "schedule", "meeting", "appointment", "event",
        "tomorrow", "today's schedule", "upcoming",
        "this week", "next week", "free time", "busy",
    ],
    "reminder": [
        "remind", "reminds", "reminder", "reminders", "remind me",
        "set a reminder", "delete reminder",
    ],
    "memory": [
        "search memory", "search memories", "search your memory",
        "search your memories", "memory search", "recall", "do you remember",
        "what do you know about", "save to memory", "memory save",
        "remember this", "memory recall", "remember that",
        "my email", "my phone", "my address",
    ],
    "email": [
        "email", "mail", "inbox", "unread", "send an email", "send email",
        "compose", "message to", "gmail",
    ],
    "web_search": [
        "search", "find on the web", "look up", "google", "web search",
        "search the web", "browse", "news about",
    ],
    "file": [
        "read file", "write file", "open file", "save file", "create file",
        "edit file", "delete file", "copy file", "move file",
        "read the file", "write to", "file called",
        "to a file", "save to a file",
    ],
    "shell": [
        "list files", "list all", "directory", "run command", "execute",
        "terminal", "command", "ls", "dir", "show files", "current directory",
        "folder contents",
    ],
    "open_app": [
        "open app", "launch", "start application", "run app",
        "start the", "launch the", "open the application",
    ],
    "screenshot": [
        "screenshot", "screen capture", "capture screen", "take screenshot",
        "screen shot",
    ],
    "time": [
        "what time", "current time", "what's the time", "time is it",
        "date and time", "what day", "today's date", "what is today",
    ],
    "weather": [
        "weather", "temperature", "forecast", "rain", "sunny",
        "cloudy", "humid", "wind speed",
    ],
    "chat": [
        "joke", "story", "tell me", "what is the meaning", "explain",
        "help me", "who are you", "hello", "hi ", "thanks", "thank you",
        "how are you",
        "my favorite", "my name is", "what is my", "what do i", "what am i",
        "do i like", "how old am i", "where do i", "what grade",
    ],
}


NEGATIVE_PATTERNS = {
    "file": ["already read", "thread", "spreadsheet"],
    "calendar": ["calendar says", "calendar year"],
}


class IntentClassifier:
    """Classifies user input into intent categories using Groq LLM + keyword fallback."""

    def __init__(self, groq_api_key=None):
        self.groq_key = groq_api_key or os.environ.get("GROQ_API_KEY", "")
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        self.model = "llama-3.1-8b-instant"
        self.categories = CATEGORIES
        self.categories_str = ", ".join(self.categories)

    def classify(self, user_input: str) -> dict:
        """
        Classify user input into an intent category.
        Returns: {"category": str, "confidence": float, "method": "llm"|"keyword"}
        """
        # Try keyword matching first (fast, no API call)
        keyword_result = self._keyword_match(user_input)
        if keyword_result["confidence"] >= 0.6:
            return keyword_result

        # Fall back to LLM
        llm_result = self._llm_classify(user_input)
        if llm_result:
            return llm_result

        # Ultimate fallback to keyword (lower threshold)
        return keyword_result

    def _keyword_match(self, user_input: str) -> dict:
        """Fast local keyword matching."""
        text = user_input.lower()
        scores = {}

        for category, keywords in KEYWORD_MAP.items():
            score = 0
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text):
                    # Longer keywords are more specific
                    score += len(kw)
            if score > 0:
                scores[category] = score

        # File extensions are a strong indicator of file intent
        if re.search(r'\.(txt|json|csv|md|py|js|html|css|xml|yaml|log|cfg|ini|toml)', text):
            scores["file"] = scores.get("file", 0) + 10

        # Apply negative pattern penalties
        for category in list(scores.keys()):
            if category in NEGATIVE_PATTERNS:
                for pattern in NEGATIVE_PATTERNS[category]:
                    if pattern in text:
                        scores[category] -= 30
                        if scores[category] <= 0:
                            del scores[category]
                        break

        if not scores:
            return {"category": "chat", "confidence": 0.3, "method": "keyword"}

        best = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best] / total if total > 0 else 0.3

        return {"category": best, "confidence": round(confidence, 2), "method": "keyword"}

    def _llm_classify(self, user_input: str) -> dict:
        """LLM-based classification via Groq."""
        if not self.groq_key:
            return None

        prompt = (
            f"Classify this task into exactly ONE category: [{self.categories_str}]. "
            f"Return ONLY the category name, nothing else.\n"
            f"Task: {user_input}"
        )

        try:
            r = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.1,
                },
                headers={"Authorization": f"Bearer {self.groq_key}"},
                timeout=10,
            )

            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"].strip().lower()
                content = content.strip().lower()
                # Extract the first word as category (handles "web search" -> "web_search")
                parts = re.split(r'[^a-z_]', content)
                content = parts[0] if parts else ""

                if content in self.categories:
                    return {
                        "category": content,
                        "confidence": 0.9,
                        "method": "llm",
                    }
                # Partial match
                for cat in self.categories:
                    if cat in content or content in cat:
                        return {
                            "category": cat,
                            "confidence": 0.7,
                            "method": "llm",
                        }

                return {
                    "category": "chat",
                    "confidence": 0.4,
                    "method": "llm",
                }
            else:
                logger.warning(f"Groq API error: {r.status_code}")
                return None

        except Exception as e:
            logger.warning(f"LLM classification failed: {e}")
            return None

    def classify_multi(self, user_input: str) -> list:
        """
        Detect if a command requires multiple steps and return ordered categories.
        E.g., "Search for Python tutorials and save to file" -> ["web_search", "file"]
        """
        # Check for conjunction words that indicate multi-step
        conjunctions = [" and ", " then ", " after that ", "; ", " followed by "]
        has_conjunction = any(c in user_input.lower() for c in conjunctions)

        if not has_conjunction:
            result = self.classify(user_input)
            return [result]

        # Split by conjunctions and classify each part
        parts = re.split(r"(?:\band\b|\bthen\b|\bafter that\b|;|\bfollowed by\b)", user_input, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if p.strip()]

        results = []
        for part in parts:
            result = self.classify(part)
            results.append(result)

        return results


# Test
if __name__ == "__main__":
    import sys
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    classifier = IntentClassifier()

    tests = [
        "What is 42 times 8?",
        "Check my calendar for tomorrow",
        "Find my unread emails",
        "Search the web for Python web scraping",
        "Read config.json",
        "List all files in current directory",
        "Tell me a joke",
        "Open the calculator app",
        "Take a screenshot",
        "Search for AI news and save top 3 articles to a file",
        "What is 15 + 27 and then send me the result by email",
    ]

    print(f"Intent Classifier Tests (model: {classifier.model})")
    print("=" * 70)

    for task in tests:
        result = classifier.classify(task)
        print(f"  {task[:55]:55s} -> {result['category']:12s} (conf: {result['confidence']}, {result['method']})")

    print("\nMulti-step detection:")
    multi = "Search for Python tutorials and save top 3 URLs to a file"
    results = classifier.classify_multi(multi)
    cats = [r["category"] for r in results]
    print(f"  {multi[:55]:55s} -> {cats}")
