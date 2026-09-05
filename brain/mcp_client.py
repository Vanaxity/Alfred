"""
MCP Client — generic Model Context Protocol client.

Connects to any MCP server listed in mcp_servers.json (repo root) -- the
same config shape every MCP-compatible client already uses:
    {"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}

Discovers each server's tools via tools/list and wraps them into Alfred's
own ToolResult shape so they register through the existing
ToolExecutor.register() (brain/v2/tool_executor.py) -- no new
registration mechanism, and critically, no per-server hardcoded
integration code. A Slack MCP server, a Telegram one, or one nobody's
heard of yet all wire up exactly the same way: an entry in the config
file, not new Python.

Tool names are registered as "<server>__<tool>" to avoid collisions
between servers that happen to expose a same-named tool.
"""
from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .v2.tool_executor import ToolResult

CONFIG_PATH = Path(__file__).parent.parent / "mcp_servers.json"


class MCPClientManager:
    """Owns one long-lived ClientSession per configured MCP server.

    Connect once at process startup (see brain_api/server.py's lifespan
    handler), not per-call -- matches how the rest of Alfred's tools are
    already long-lived, not respawned per turn.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, ClientSession] = {}
        self._stack = AsyncExitStack()
        self._connected = False

    async def connect_all(self) -> List[Tuple[str, str, Any]]:
        """Spawn every configured server, discover its tools. Returns
        (server_name, tool_name, mcp Tool) for everything wired up.
        Idempotent -- a second call is a no-op if already connected."""
        if self._connected:
            return []
        self._connected = True

        if not CONFIG_PATH.exists():
            return []
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [MCP] Failed to parse {CONFIG_PATH.name}: {e}")
            return []

        discovered: List[Tuple[str, str, Any]] = []
        for name, spec in (config.get("mcpServers") or {}).items():
            try:
                merged_env = None
                if spec.get("env"):
                    merged_env = {**os.environ, **spec["env"]}
                params = StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env=merged_env,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session

                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    discovered.append((name, tool.name, tool))
                print(f"  [MCP] Connected '{name}': {len(tools_result.tools)} tool(s)")
            except Exception as e:
                # One misbehaving server must not take down the others, or
                # Alfred itself -- MCP servers are third-party processes.
                print(f"  [MCP] Failed to connect '{name}': {e}")
        return discovered

    async def disconnect_all(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
        self._connected = False

    def make_handler(self, server_name: str, tool_name: str):
        """A ToolExecutor-compatible async handler closing over which
        server/tool this specific call routes to."""

        async def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
            session = self._sessions.get(server_name)
            if session is None:
                return ToolResult(
                    success=False,
                    error=f"MCP server '{server_name}' is not connected",
                )
            try:
                result = await session.call_tool(tool_name, params or {})
            except Exception as e:
                return ToolResult(success=False, error=f"MCP call failed: {e}")

            text_parts = [
                block.text
                for block in (result.content or [])
                if getattr(block, "type", None) == "text"
            ]
            if text_parts:
                output = "\n".join(text_parts)[:3000]
            else:
                output = str(result.structured_content or "")[:3000]

            if result.is_error:
                return ToolResult(success=False, error=output or "MCP tool returned an error")
            return ToolResult(success=True, output=output)

        return _handler


_manager: Optional[MCPClientManager] = None


def get_mcp_client() -> MCPClientManager:
    global _manager
    if _manager is None:
        _manager = MCPClientManager()
    return _manager
