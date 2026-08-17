"""
Alfred v2 — Thin wrapper that delegates to brain.v2 (Hermes-inspired rebuild).

The actual implementation lives in brain/v2/:
    - brain.v2.conversation.Alfred  (main agent class)
    - brain.v2.prompt_builder       (structured prompt assembly)
    - brain.v2.context_manager      (token tracking + compression)
    - brain.v2.tool_executor        (modular dispatch + guardrails)
    - brain.v2.heartbeat            (cognitive reasoning + cron)

This file preserves backward compatibility for brain/__init__.py imports.
"""

# Re-export from the new v2 package
from .v2.conversation import Alfred, Response, execute_task
from .v2.prompt_builder import PromptBuilder, ToolSchema, count_tokens
from .v2.context_manager import ConversationHistory, Message, Role
from .v2.tool_executor import ToolExecutor, ToolResult, Guardrails, create_tool_executor
from .v2.heartbeat import CognitiveHeartbeat

# Backward-compatible singleton accessor
from .v2.conversation import get_alfred

__all__ = [
    "Alfred",
    "Response",
    "execute_task",
    "get_alfred",
    "PromptBuilder",
    "ToolSchema",
    "count_tokens",
    "ConversationHistory",
    "Message",
    "Role",
    "ToolExecutor",
    "ToolResult",
    "Guardrails",
    "create_tool_executor",
    "CognitiveHeartbeat",
]
