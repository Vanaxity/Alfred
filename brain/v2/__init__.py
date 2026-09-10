"""
Alfred v2 — Hermes-inspired clean agent loop.

Modules:
    prompt_builder   — Structured system prompt assembly with token budget
    context_manager  — Conversation token tracking and compression
    tool_executor    — Modular tool dispatch with guardrails and validation
    conversation     — Main LLM→tool→LLM loop with role alternation

Public API (preserves existing interface for brain_api/server.py):
    get_alfred()     — Returns the Alfred v2 singleton
    execute_task()   — Convenience async wrapper
"""

from .conversation import Alfred, execute_task
from .context_manager import ConversationHistory, Message
from .prompt_builder import (
    AssembledPrompt,
    PRIO_IDENTITY,
    PRIO_MEMORY,
    PRIO_PROFILE,
    PRIO_RULES,
    PRIO_SKILLS,
    PRIO_TOOLS,
    PromptBuilder,
    ToolSchema,
    count_tokens,
)
from .tool_executor import (
    ACTION_MUTATIONS,
    Guardrails,
    MAX_TOOL_ATTEMPTS,
    MUTATION_TOOLS,
    NO_READBACK,
    TOOL_GUARDRAILS,
    ToolExecutor,
    ToolResult,
    VERIFY_MAP,
    create_tool_executor,
)

_alfred_instance: "Alfred | None" = None


def get_alfred() -> "Alfred":
    """Return the global Alfred v2 singleton."""
    global _alfred_instance
    if _alfred_instance is None:
        _alfred_instance = Alfred()
    return _alfred_instance


__all__ = [
    "Alfred",
    "get_alfred",
    "execute_task",
    "PromptBuilder",
    "count_tokens",
    "ToolSchema",
    "AssembledPrompt",
    "ConversationHistory",
    "Message",
    "PRIO_IDENTITY",
    "PRIO_RULES",
    "PRIO_PROFILE",
    "PRIO_TOOLS",
    "PRIO_SKILLS",
    "PRIO_MEMORY",
    "ToolExecutor",
    "ToolResult",
    "Guardrails",
    "create_tool_executor",
    "MUTATION_TOOLS",
    "ACTION_MUTATIONS",
    "VERIFY_MAP",
    "NO_READBACK",
    "TOOL_GUARDRAILS",
    "MAX_TOOL_ATTEMPTS",
]
