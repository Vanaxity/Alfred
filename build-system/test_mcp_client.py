"""
MCP client unit tests -- Phase 2.

Run directly:
    python build-system/test_mcp_client.py

Covers the generic client's handler-wrapping (MCP result -> ToolResult),
the connect-with-no-config no-op path, and that discovered tools actually
reach _get_tool_descriptions() -- registering a tool with ToolExecutor
alone isn't enough, since the LLM only ever sees what that method returns.
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import brain.mcp_client as mcp_client_module  # noqa: E402
from brain.mcp_client import MCPClientManager  # noqa: E402
from brain.v2.conversation import Alfred  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes standing in for the real mcp SDK's result shapes
# ---------------------------------------------------------------------------

class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeCallToolResult:
    def __init__(self, text: str = "", is_error: bool = False, structured_content: Any = None):
        self.content = [_FakeTextBlock(text)] if text else []
        self.is_error = is_error
        self.structured_content = structured_content


class _FakeSession:
    def __init__(self, result: Optional[_FakeCallToolResult] = None, raise_exc: Optional[Exception] = None):
        self._result = result
        self._raise = raise_exc
        self.last_call: Optional[tuple] = None
        self.call_tool_ran_on_task: Optional[asyncio.Task] = None

    async def call_tool(self, name: str, arguments: Dict[str, Any]):
        self.last_call = (name, arguments)
        self.call_tool_ran_on_task = asyncio.current_task()
        if self._raise:
            raise self._raise
        return self._result


class _FakeTool:
    def __init__(self, name: str, description: str = "", input_schema: Optional[Dict] = None):
        self.name = name
        self.description = description
        self.input_schema = input_schema or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_handler_wraps_successful_text_result():
    manager = MCPClientManager()
    manager._sessions["filesystem"] = _FakeSession(_FakeCallToolResult(text="hello.txt\nworld.txt"))
    handler = manager.make_handler("filesystem", "list_directory")
    result = run(handler({"path": "."}, {}))
    assert result.success is True
    assert "hello.txt" in result.output


def test_handler_passes_params_through_to_call_tool():
    session = _FakeSession(_FakeCallToolResult(text="ok"))
    manager = MCPClientManager()
    manager._sessions["filesystem"] = session
    handler = manager.make_handler("filesystem", "read_file")
    run(handler({"path": "README.md"}, {}))
    assert session.last_call == ("read_file", {"path": "README.md"})


def test_handler_reports_mcp_is_error_as_failure():
    manager = MCPClientManager()
    manager._sessions["filesystem"] = _FakeSession(_FakeCallToolResult(text="permission denied", is_error=True))
    handler = manager.make_handler("filesystem", "write_file")
    result = run(handler({}, {}))
    assert result.success is False
    assert "permission denied" in result.error


def test_handler_catches_exceptions_from_call_tool():
    manager = MCPClientManager()
    manager._sessions["filesystem"] = _FakeSession(raise_exc=RuntimeError("server crashed"))
    handler = manager.make_handler("filesystem", "read_file")
    result = run(handler({}, {}))
    assert result.success is False
    assert "server crashed" in result.error


def test_handler_runs_call_tool_on_the_manager_worker_task():
    """Live-found bug: MCPClientManager.__init__'s own comment documents
    that entering/exiting an MCP session from a per-HTTP-request task
    (rather than the manager's one persistent worker task) breaks anyio's
    cancel-scope ownership -- already fixed for connect/disconnect by
    routing them through _run_on_worker. make_handler()'s actual
    session.call_tool() was never given the same treatment, so every real
    tool call ran on whatever task happened to be executing Alfred.execute()
    at the time -- a fresh task per request on a live server. Confirmed
    live against a real Nuclear (streamable-HTTP) MCP session: connect +
    list_tools at boot succeeded (same task), then the next real request's
    tool call failed with "Session terminated" -- exactly the cross-task
    failure this class of bug already has one fix pattern for in this file.
    """
    async def _run_test():
        manager = MCPClientManager()
        manager._ensure_worker()  # must happen inside a running loop, like real usage
        worker_task = manager._worker_task
        assert asyncio.current_task() is not worker_task, (
            "sanity check: the test's own task must differ from the worker task, "
            "or this test can't actually distinguish the two"
        )

        session = _FakeSession(_FakeCallToolResult(text="ok"))
        manager._sessions["nuclear"] = session
        handler = manager.make_handler("nuclear", "call")

        result = await handler({"method": "Playback.play"}, {})

        assert result.success is True
        assert session.call_tool_ran_on_task is worker_task, (
            f"call_tool ran on {session.call_tool_ran_on_task!r}, not the "
            f"manager's worker task {worker_task!r} -- this is the same "
            "cross-task cancel-scope bug already fixed for connect/disconnect"
        )

    run(_run_test())


def test_handler_for_unconnected_server_fails_cleanly():
    manager = MCPClientManager()
    handler = manager.make_handler("never_connected", "some_tool")
    result = run(handler({}, {}))
    assert result.success is False
    assert "not connected" in result.error


def test_connect_all_with_no_config_file_is_a_safe_noop():
    manager = MCPClientManager()
    manager_config_path_missing = Path("/definitely/does/not/exist/mcp_servers.json")
    import brain.mcp_client as mcp_client_module
    original = mcp_client_module.CONFIG_PATH
    mcp_client_module.CONFIG_PATH = manager_config_path_missing
    try:
        discovered = run(manager.connect_all())
        assert discovered == []
    finally:
        mcp_client_module.CONFIG_PATH = original


def test_connect_one_returns_empty_list_and_does_not_raise_on_spawn_failure():
    """connect_all()'s per-server try/except was extracted into
    connect_one() so a live install can target a single new server --
    this checks the extraction kept the 'one bad server can't take down
    Alfred' behavior."""
    manager = MCPClientManager()
    discovered = run(manager.connect_one(
        "broken", {"command": "definitely-not-a-real-executable-xyz", "args": []},
    ))
    assert discovered == []


def test_connect_all_delegates_to_connect_one_per_server(monkeypatch=None):
    """connect_all() should now be a thin loop over connect_one() -- verify
    it still aggregates results from multiple configured servers rather
    than the refactor silently dropping the loop body."""
    manager = MCPClientManager()
    calls = []

    async def fake_connect_one(name, spec):
        calls.append(name)
        return [(name, "some_tool", object())]

    manager.connect_one = fake_connect_one
    import brain.mcp_client as mcp_client_module
    import json
    import tempfile
    import os as _os

    fd, path = tempfile.mkstemp(suffix=".json")
    _os.close(fd)
    Path(path).write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}))
    original = mcp_client_module.CONFIG_PATH
    mcp_client_module.CONFIG_PATH = Path(path)
    try:
        discovered = run(manager.connect_all())
    finally:
        mcp_client_module.CONFIG_PATH = original
        _os.remove(path)

    assert sorted(calls) == ["a", "b"]
    assert len(discovered) == 2


def test_connect_all_is_idempotent():
    manager = MCPClientManager()
    manager._connected = True  # simulate already having connected
    discovered = run(manager.connect_all())
    assert discovered == [], "a second connect_all() call must be a no-op, not reconnect"


class _FakeToolsResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeHttpSession:
    """Stands in for ClientSession(read, write) -- used as an async context
    manager in the real code (local_stack.enter_async_context(...)), so
    this needs __aenter__/__aexit__ too, not just initialize()/list_tools()."""

    def __init__(self, tools):
        self._tools = tools

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeToolsResult(self._tools)


def test_connect_one_uses_http_transport_when_spec_has_a_url():
    """A server config keyed by "url" instead of "command" (e.g. Nuclear's
    music-player MCP server) must go through streamable_http_client, not
    stdio_client -- and must NOT run the stdio path's shutil.which
    precheck, which only makes sense for a local command to spawn."""
    calls = {"http_urls": [], "stdio_commands": []}

    @asynccontextmanager
    async def fake_streamable_http_client(url):
        calls["http_urls"].append(url)
        yield ("fake_read", "fake_write")

    @asynccontextmanager
    async def fake_stdio_client(params):
        calls["stdio_commands"].append(params.command)
        yield ("fake_read", "fake_write")

    def fake_client_session(read, write):
        return _FakeHttpSession([_FakeTool("call", "Execute a Nuclear API method")])

    original_http = mcp_client_module.streamable_http_client
    original_stdio = mcp_client_module.stdio_client
    original_session = mcp_client_module.ClientSession
    mcp_client_module.streamable_http_client = fake_streamable_http_client
    mcp_client_module.stdio_client = fake_stdio_client
    mcp_client_module.ClientSession = fake_client_session
    try:
        manager = MCPClientManager()
        discovered = run(manager.connect_one("nuclear", {"url": "http://127.0.0.1:8800/mcp"}))
    finally:
        mcp_client_module.streamable_http_client = original_http
        mcp_client_module.stdio_client = original_stdio
        mcp_client_module.ClientSession = original_session

    assert calls["http_urls"] == ["http://127.0.0.1:8800/mcp"]
    assert calls["stdio_commands"] == [], "a url-keyed spec must never touch the stdio path"
    assert len(discovered) == 1
    assert discovered[0] == ("nuclear", "call", discovered[0][2])


def test_connect_one_still_uses_stdio_for_a_command_spec():
    """Regression check alongside the http test above: an ordinary
    command-keyed spec (every existing server) must still take the stdio
    path, not get accidentally routed through http just because the http
    branch now exists."""
    calls = {"http_urls": [], "stdio_commands": []}

    @asynccontextmanager
    async def fake_streamable_http_client(url):
        calls["http_urls"].append(url)
        yield ("fake_read", "fake_write")

    @asynccontextmanager
    async def fake_stdio_client(params):
        calls["stdio_commands"].append(params.command)
        yield ("fake_read", "fake_write")

    def fake_client_session(read, write):
        return _FakeHttpSession([_FakeTool("list_directory")])

    original_http = mcp_client_module.streamable_http_client
    original_stdio = mcp_client_module.stdio_client
    original_session = mcp_client_module.ClientSession
    original_which = mcp_client_module.shutil.which
    mcp_client_module.streamable_http_client = fake_streamable_http_client
    mcp_client_module.stdio_client = fake_stdio_client
    mcp_client_module.ClientSession = fake_client_session
    mcp_client_module.shutil.which = lambda cmd: "/usr/bin/npx"  # pretend it exists
    try:
        manager = MCPClientManager()
        discovered = run(manager.connect_one("filesystem", {"command": "npx", "args": ["-y", "pkg"]}))
    finally:
        mcp_client_module.streamable_http_client = original_http
        mcp_client_module.stdio_client = original_stdio
        mcp_client_module.ClientSession = original_session
        mcp_client_module.shutil.which = original_which

    assert calls["stdio_commands"] == ["npx"]
    assert calls["http_urls"] == [], "a command-keyed spec must never touch the http path"
    assert len(discovered) == 1


def test_discovered_tools_reach_get_tool_descriptions():
    """Registering a tool with ToolExecutor alone isn't enough -- the LLM
    only ever sees what _get_tool_descriptions() returns. This is the
    actual integration point that would silently make MCP tools
    unreachable if missed."""
    Stub = type("S", (), {
        "_get_tool_descriptions": Alfred.__dict__["_get_tool_descriptions"],
    })
    s = Stub()
    s._mcp_tool_schemas = {
        "filesystem__read_file": {
            "description": "Read a file. (Requires approval before running -- third-party MCP server.)",
            "params": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    }
    descriptions = s._get_tool_descriptions()
    assert "filesystem__read_file" in descriptions
    assert "chat" in descriptions, "built-in tools must still be present alongside MCP ones"
    assert "Requires approval" in descriptions["filesystem__read_file"]["description"]


def test_no_mcp_tools_leaves_builtin_descriptions_unchanged():
    Stub = type("S", (), {
        "_get_tool_descriptions": Alfred.__dict__["_get_tool_descriptions"],
    })
    s = Stub()
    s._mcp_tool_schemas = {}
    descriptions = s._get_tool_descriptions()
    assert "chat" in descriptions
    assert not any("__" in name for name in descriptions), "no MCP-style names should leak in with no servers connected"


def main():
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
            passed += 1
        except Exception:
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} mcp_client tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
