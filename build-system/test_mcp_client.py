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
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

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

    async def call_tool(self, name: str, arguments: Dict[str, Any]):
        self.last_call = (name, arguments)
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
