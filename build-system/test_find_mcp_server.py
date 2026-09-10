"""
find_mcp_server / install_mcp_server unit tests -- Phase 2 stretch.

Run directly:
    python build-system/test_find_mcp_server.py

Covers the registry-search handler's parsing/filtering of a fake
registry response, its failure paths, and install_mcp_server's
param-validation and success/failure wiring through ctx (the handler
itself has no instance access -- it only ever calls the callback
Alfred hands it via tool_ctx["install_mcp_server"], same pattern as
memory/router/bootstrap).
"""
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

import brain.v2.tool_executor as tool_executor  # noqa: E402
from brain.v2.tool_executor import (  # noqa: E402
    handle_find_mcp_server,
    handle_install_mcp_server,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake requests module for handle_find_mcp_server
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequestsModule:
    """Stands in for `import requests` inside the handler -- monkeypatched
    into sys.modules so the handler's local import picks it up."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.last_call = None

    def get(self, url, params=None, timeout=None):
        self.last_call = (url, params, timeout)
        if self._raise:
            raise self._raise
        return self._response


_REGISTRY_PAYLOAD = {
    "servers": [
        {
            "server": {
                "name": "io.example/slack",
                "description": "Send and read Slack messages.",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "slack-mcp-server",
                        "environmentVariables": [
                            {"name": "SLACK_BOT_TOKEN", "isRequired": True, "isSecret": True},
                            {"name": "SLACK_TEAM_ID", "isRequired": False, "isSecret": False},
                        ],
                    }
                ],
            }
        },
        {
            # No npm package -- e.g. docker-only. Must be filtered out since
            # mcp_client.py can only stdio-spawn command/args, not containers.
            "server": {
                "name": "io.example/docker-only",
                "description": "A server we cannot actually run.",
                "packages": [{"registryType": "docker", "identifier": "docker-only-mcp"}],
            }
        },
        {
            "server": {
                "name": "io.example/filesystem",
                "description": "Filesystem access.",
                "packages": [{"registryType": "npm", "identifier": "@modelcontextprotocol/server-filesystem"}],
            }
        },
    ]
}


def _patch_requests(monkey_module):
    import sys as _sys
    original = _sys.modules.get("requests")
    _sys.modules["requests"] = monkey_module
    return original


def _restore_requests(original):
    import sys as _sys
    if original is None:
        _sys.modules.pop("requests", None)
    else:
        _sys.modules["requests"] = original


def test_find_mcp_server_requires_query():
    result = run(handle_find_mcp_server({}, {}))
    assert result.success is False
    assert "query" in result.error


def test_find_mcp_server_filters_non_npm_and_surfaces_secret_env():
    fake = _FakeRequestsModule(response=_FakeResponse(_REGISTRY_PAYLOAD))
    original = _patch_requests(fake)
    try:
        result = run(handle_find_mcp_server({"query": "slack"}, {}))
    finally:
        _restore_requests(original)

    assert result.success is True
    assert "docker-only" not in result.output, "non-npm packages must be filtered out"
    assert "slack-mcp-server" in result.output
    assert "SLACK_BOT_TOKEN" in result.output
    assert "secret" in result.output.lower(), "a required+secret env var must be flagged as secret, not just required"
    assert fake.last_call[1] == {"search": "slack", "limit": 5}


def test_find_mcp_server_caps_at_three_candidates():
    payload = {
        "servers": [
            {"server": {"name": f"io.example/s{i}", "description": "x",
                        "packages": [{"registryType": "npm", "identifier": f"pkg-{i}"}]}}
            for i in range(5)
        ]
    }
    fake = _FakeRequestsModule(response=_FakeResponse(payload))
    original = _patch_requests(fake)
    try:
        result = run(handle_find_mcp_server({"query": "x"}, {}))
    finally:
        _restore_requests(original)
    assert result.success is True
    assert result.output.count("pkg-") == 3


def test_find_mcp_server_no_npm_candidates_is_a_clean_failure():
    payload = {"servers": [{"server": {"name": "x", "description": "x",
                                        "packages": [{"registryType": "docker", "identifier": "x"}]}}]}
    fake = _FakeRequestsModule(response=_FakeResponse(payload))
    original = _patch_requests(fake)
    try:
        result = run(handle_find_mcp_server({"query": "x"}, {}))
    finally:
        _restore_requests(original)
    assert result.success is False
    assert "No installable" in result.error


def test_find_mcp_server_network_failure_becomes_tool_error_not_a_crash():
    fake = _FakeRequestsModule(raise_exc=ConnectionError("no route to host"))
    original = _patch_requests(fake)
    try:
        result = run(handle_find_mcp_server({"query": "slack"}, {}))
    finally:
        _restore_requests(original)
    assert result.success is False
    assert "no route to host" in result.error


# ---------------------------------------------------------------------------
# handle_install_mcp_server -- thin wrapper around ctx["install_mcp_server"]
# ---------------------------------------------------------------------------

def test_install_mcp_server_requires_name_and_command():
    result = run(handle_install_mcp_server({"name": "x"}, {"install_mcp_server": None}))
    assert result.success is False


def test_install_mcp_server_missing_from_context_fails_cleanly():
    result = run(handle_install_mcp_server({"name": "x", "command": "npx"}, {}))
    assert result.success is False
    assert "not available" in result.error


def test_install_mcp_server_calls_callback_with_params_and_reports_new_tools():
    calls = []

    async def fake_install(name, command, args, env):
        calls.append((name, command, args, env))
        return ["slack__send_message", "slack__list_channels"]

    result = run(handle_install_mcp_server(
        {"name": "slack", "command": "npx", "args": ["-y", "slack-mcp-server"], "env": {"SLACK_BOT_TOKEN": "xoxb-x"}},
        {"install_mcp_server": fake_install},
    ))
    assert result.success is True
    assert "slack__send_message" in result.output
    assert calls == [("slack", "npx", ["-y", "slack-mcp-server"], {"SLACK_BOT_TOKEN": "xoxb-x"})]


def test_install_mcp_server_no_tools_discovered_is_a_failure():
    async def fake_install(name, command, args, env):
        return []

    result = run(handle_install_mcp_server(
        {"name": "broken", "command": "npx", "args": []},
        {"install_mcp_server": fake_install},
    ))
    assert result.success is False


def test_install_mcp_server_propagates_callback_exception_as_tool_error():
    async def fake_install(name, command, args, env):
        raise RuntimeError("npx: command not found")

    result = run(handle_install_mcp_server(
        {"name": "x", "command": "npx", "args": []},
        {"install_mcp_server": fake_install},
    ))
    assert result.success is False
    assert "npx: command not found" in result.error


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
    print(f"\n{passed}/{len(tests)} find_mcp_server tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
