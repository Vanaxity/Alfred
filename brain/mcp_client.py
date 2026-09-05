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

import asyncio
import json
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .v2.tool_executor import ToolResult

CONFIG_PATH = Path(__file__).parent.parent / "mcp_servers.json"

# A bad command can take a surprisingly long time to fail outright (a
# nonexistent executable took 20-100+s to surface WinError 2 in testing,
# rather than failing immediately) and a live-added server (via
# install_mcp_server) is inherently less vetted than one hand-configured
# at startup. Bound the whole spawn+handshake so a bad or unresponsive
# server can't stall the conversation turn that's waiting on it.
CONNECT_TIMEOUT_SECONDS = 20


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
        # anyio (which the mcp SDK uses under the hood) ties a cancel scope
        # to the specific asyncio Task that opened it. connect_all() at
        # startup runs on the app lifespan's own long-lived task, so
        # entering stdio_client/ClientSession there and exiting them later
        # from that same task (disconnect_all(), also lifespan-driven) is
        # safe. install_mcp_server(), though, runs from a per-HTTP-request
        # task that finishes as soon as the response is sent -- confirmed
        # live: entering the session there raised "Attempted to exit a
        # cancel scope that isn't the current task's current cancel scope"
        # once the request task wound down, which killed the subprocess
        # ("Connection closed" on the very next tool call). Every actual
        # enter/exit now happens on one persistent worker task instead, so
        # it doesn't matter which task calls connect_one()/disconnect_all().
        self._worker_queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None

    def _ensure_worker(self) -> None:
        if self._worker_task is None:
            self._worker_queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            item = await self._worker_queue.get()
            if item is None:
                return
            coro_fn, fut = item
            try:
                result = await coro_fn()
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)

    async def _run_on_worker(self, coro_fn):
        """Run coro_fn() on the manager's single persistent worker task and
        return its result, regardless of which task calls this."""
        self._ensure_worker()
        fut = asyncio.get_running_loop().create_future()
        await self._worker_queue.put((coro_fn, fut))
        return await fut

    async def connect_one(self, name: str, spec: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
        """Spawn a single server and discover its tools. Used both by
        connect_all() at startup and by a live install (find_mcp_server /
        install_mcp_server) adding one server without restarting Alfred.
        Returns [] (not a raised exception) on failure -- one misbehaving
        server must not take down the others or Alfred itself, since MCP
        servers are third-party processes."""
        return await self._run_on_worker(lambda: self._connect_one_impl(name, spec))

    async def _connect_one_impl(self, name: str, spec: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
        """The actual spawn+handshake -- always runs on the worker task
        (see _run_on_worker), never called directly.

        Uses its own AsyncExitStack, not self._stack directly, so a
        timeout partway through can clean up whatever was opened without
        disturbing already-connected servers -- only transferred into the
        shared stack (for disconnect_all()) once connection fully
        succeeds."""
        # A command that doesn't resolve to a real executable can take far
        # longer to fail via actual process spawn than the timeout below
        # allows for -- confirmed live on Windows: a bad command name hits
        # an OS command-resolution shim (the "look this up in the Store?"
        # path) that can block the event loop itself for 20-100+s, well
        # past what asyncio.wait_for can preempt since the block isn't a
        # cancellable await. shutil.which is a plain PATH/PATHEXT scan
        # with none of that, and resolves in milliseconds either way.
        if shutil.which(spec.get("command", "")) is None:
            print(f"  [MCP] Failed to connect '{name}': command not found: {spec.get('command')!r}")
            return []

        local_stack = AsyncExitStack()

        async def _do_connect():
            merged_env = None
            if spec.get("env"):
                merged_env = {**os.environ, **spec["env"]}
            params = StdioServerParameters(
                command=spec["command"],
                args=spec.get("args", []),
                env=merged_env,
            )
            read, write = await local_stack.enter_async_context(stdio_client(params))
            session = await local_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_result = await session.list_tools()
            return session, tools_result

        try:
            session, tools_result = await asyncio.wait_for(
                _do_connect(), timeout=CONNECT_TIMEOUT_SECONDS,
            )
            self._sessions[name] = session
            # Hand cleanup ownership to the manager's long-lived stack so
            # disconnect_all() still closes this server later.
            self._stack.push_async_callback(local_stack.pop_all().aclose)

            discovered = [(name, tool.name, tool) for tool in tools_result.tools]
            print(f"  [MCP] Connected '{name}': {len(tools_result.tools)} tool(s)")
            return discovered
        except asyncio.TimeoutError:
            await local_stack.aclose()
            print(f"  [MCP] Failed to connect '{name}': timed out after {CONNECT_TIMEOUT_SECONDS}s")
            return []
        except Exception as e:
            await local_stack.aclose()
            print(f"  [MCP] Failed to connect '{name}': {e}")
            return []

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
            discovered.extend(await self.connect_one(name, spec))
        return discovered

    async def disconnect_all(self) -> None:
        if self._worker_task is not None:
            # Close on the same worker task that opened everything -- see
            # __init__'s note on cancel-scope ownership.
            await self._run_on_worker(self._stack.aclose)
            await self._worker_queue.put(None)
            await self._worker_task
            self._worker_task = None
            self._worker_queue = None
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
