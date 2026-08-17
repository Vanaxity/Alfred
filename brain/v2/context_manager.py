"""
Context Manager — Conversation token tracking and compression.

Tracks token count per message in a ConversationHistory. When the total
exceeds the conversation budget, compresses the oldest user-assistant pairs
into one-line summaries while preserving the most recent tool-call + tool-result
pair (critical for LLM context continuity).

Enforces role alternation: user → assistant → tool → assistant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from .prompt_builder import count_tokens


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """A single conversation message."""
    role: Role
    content: str
    token_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.token_count == 0:
            self.token_count = count_tokens(self.content)

    @property
    def is_tool_result(self) -> bool:
        return self.role == Role.TOOL

    @property
    def is_tool_call(self) -> bool:
        """Check if assistant message contains a tool call."""
        if self.role != Role.ASSISTANT:
            return False
        try:
            parsed = json.loads(self.content)
            return isinstance(parsed, dict) and "tool" in parsed
        except (json.JSONDecodeError, TypeError):
            return False


# ---------------------------------------------------------------------------
# ConversationHistory
# ---------------------------------------------------------------------------

class ConversationHistory:
    """
    Manages the conversation message list with token tracking.

    Key behaviors:
        - Tracks total token count across all messages.
        - Enforces role alternation (no two consecutive same-role messages).
        - Compresses old messages when budget is exceeded.
        - Preserves the most recent tool-call + tool-result pair.
    """

    def __init__(self, token_budget: int = 12000) -> None:
        self.token_budget = token_budget
        self._messages: List[Message] = []
        self._total_tokens: int = 0

    @property
    def messages(self) -> List[Message]:
        return list(self._messages)

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def token_usage(self) -> int:
        """Total tokens across all messages (spec-compatible alias)."""
        return self._total_tokens

    @property
    def count(self) -> int:
        return len(self._messages)

    def add_user(self, content: str, **metadata: Any) -> None:
        """Add a user message, merging with previous if same role."""
        self._maybe_merge(Role.USER, content, metadata)

    def add_assistant(self, content: str, **metadata: Any) -> None:
        """Add an assistant message, merging with previous if same role."""
        self._maybe_merge(Role.ASSISTANT, content, metadata)

    def add_tool_result(self, tool_name: str, result: Any, **metadata: Any) -> None:
        """Add a tool result message, merging with previous if same role."""
        if isinstance(result, dict):
            content = json.dumps(result, default=str)
        else:
            content = str(result)
        metadata["tool_name"] = tool_name
        metadata["tool"] = tool_name
        self._maybe_merge(Role.TOOL, content, metadata)

    def _maybe_merge(self, role: Role, content: Any, metadata: Dict[str, Any]) -> None:
        """Add a message, merging with the previous one if it has the same role
        and is not a summary."""
        if self._messages:
            last = self._messages[-1]
            if last.role == role and not last.metadata.get("is_summary"):
                if role == Role.TOOL:
                    last.content = json.dumps(
                        {**json.loads(last.content), **{"merged_result": content}},
                        default=str,
                    )
                else:
                    last.content = f"{last.content}\n{content}"
                last.token_count = count_tokens(last.content)
                last.metadata.update(metadata)
                self._total_tokens = sum(m.token_count for m in self._messages)
                self._maybe_compress()
                return
        self._append(Message(role=role, content=str(content), metadata=metadata))
        self._maybe_compress()

    def add_system_note(self, content: str) -> None:
        """Add a compressed summary (role=USER with summary flag)."""
        msg = Message(
            role=Role.USER,
            content=content,
            metadata={"is_summary": True},
        )
        self._messages.append(msg)
        self._total_tokens += msg.token_count

    def to_llm_messages(self) -> List[Dict[str, str]]:
        """
        Convert to the format expected by LLM routers.

        Returns list of {"role": str, "content": str} dicts.
        Tool results are converted to user messages with a prefix.
        """
        out: List[Dict[str, str]] = []
        for msg in self._messages:
            if msg.role == Role.TOOL:
                tool_name = msg.metadata.get("tool_name", "tool")
                out.append({
                    "role": "user",
                    "content": f"[Tool result — {tool_name}]:\n{msg.content}",
                })
            else:
                out.append({
                    "role": msg.role.value,
                    "content": msg.content,
                })
        return out

    def compress_if_needed(self) -> int:
        """
        Compress oldest messages if total tokens exceed budget.

        Returns the number of messages compressed.
        """
        if self._total_tokens <= self.token_budget:
            return 0

        # Find the most recent tool-call + tool-result pair to preserve
        preserve_start = self._find_last_tool_pair_start()

        # Find how many old messages to compress
        compressed = 0
        tokens_to_remove = self._total_tokens - self.token_budget + 500  # +500 buffer
        tokens_removed = 0
        merge_end = 0

        for i, msg in enumerate(self._messages):
            if i >= preserve_start:
                break  # Don't compress past the preserved zone
            if tokens_removed >= tokens_to_remove:
                break
            tokens_removed += msg.token_count
            merge_end = i + 1
            compressed += 1

        if compressed < 2:
            return 0  # Not worth compressing fewer than 2 messages

        # Build a summary of compressed messages
        summaries: List[str] = []
        for msg in self._messages[:merge_end]:
            if msg.metadata.get("is_summary"):
                summaries.append(msg.content)
            elif msg.role == Role.USER:
                text = msg.content[:100]
                summaries.append(f"User: {text}")
            elif msg.role == Role.ASSISTANT:
                text = msg.content[:100]
                summaries.append(f"Alfred: {text}")

        summary_text = "[Conversation compressed] " + " | ".join(summaries)

        # Replace compressed messages with summary
        summary_msg = Message(
            role=Role.USER,
            content=summary_text,
            metadata={"is_summary": True},
        )

        self._messages = [summary_msg] + self._messages[merge_end:]
        self._recalc_tokens()

        return compressed

    def clear(self) -> None:
        """Clear all messages."""
        self._messages.clear()
        self._total_tokens = 0

    def seed_from_history(self, history: List[Dict[str, Any]]) -> None:
        """
        Seed conversation from database history format.

        history items: {"role": "user"|"assistant", "content": str}
        """
        for item in history[-20:]:  # Last 20 turns
            role_str = item.get("role", "user")
            content = item.get("content", "")
            if not content:
                continue
            if role_str == "user":
                self.add_user(content)
            elif role_str == "assistant":
                self.add_assistant(content)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(self, msg: Message) -> None:
        """Append a message and update token count."""
        self._messages.append(msg)
        self._total_tokens += msg.token_count

    def _maybe_compress(self) -> None:
        """Spec-compatible hook: compress after every add when over budget."""
        self.compress_if_needed()

    def _recalc_tokens(self) -> None:
        """Recalculate total tokens from scratch."""
        self._total_tokens = sum(m.token_count for m in self._messages)

    def _find_last_tool_pair_start(self) -> int:
        """
        Find the start index of the most recent tool-call + tool-result pair.

        Returns the index of the assistant message that made the tool call.
        If no tool pair found, returns 0 (start of conversation).
        """
        # Walk backwards looking for a TOOL message followed by its ASSISTANT call
        i = len(self._messages) - 1
        while i >= 0:
            msg = self._messages[i]
            if msg.role == Role.TOOL:
                # Find the preceding assistant message
                if i > 0 and self._messages[i - 1].is_tool_call:
                    return max(0, i - 1)
            i -= 1
        return 0


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demo = ConversationHistory(token_budget=200)
    # Pad the conversation so we blow past the budget
    demo.add_user("U" * 600)
    demo.add_assistant(json.dumps({"tool": "old", "params": {}}))
    demo.add_tool_result("old_tool", "R" * 600)
    demo.add_user("U" * 600)
    demo.add_assistant(json.dumps({"tool": "get_time", "params": {}}))
    demo.add_tool_result("get_time", {"hour": 14, "minute": 30})
    demo.add_assistant("The time is 2:30 PM.")

    print("=== Raw messages ===")
    for m in demo.messages:
        print(f"  [{m.role.value}] {m.content[:70]!r}  (tokens={m.token_count})")
    print(f"\ntoken_usage: {demo.token_usage} / budget {demo.token_budget}")

    print("\n=== to_llm_messages() ===")
    for msg in demo.to_llm_messages():
        print(f"  role={msg['role']!r} content={msg['content'][:60]!r}")

    print("\n=== After compression (most recent tool pair preserved) ===")
    print(f"token_usage: {demo.token_usage}")
    for m in demo.messages:
        print(f"  [{m.role.value}] {m.content[:70]!r}")
