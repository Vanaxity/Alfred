"""
ToolExecutor unit tests — Day 4/5.

Run directly:
    python build-system/test_tool_executor.py

Covers:
  1.  Unknown tool is rejected without invoking anything
  2.  Handler returning a non-ToolResult is caught, not crashed on
  3.  Guardrails: allowed=False disables a tool
  4.  Guardrails: deny_patterns block, even with approval granted
  5.  Guardrails: require_approval gates until context grants it
  6.  Guardrails: allowed_patterns act as an allowlist
  7.  Retry: mutating call recovers via LLM-corrected params
  8.  Retry: exhausts MAX_TOOL_ATTEMPTS and reports every attempt
  9.  Retry: non-mutating call is never retried
  10. Retry: identical corrected params short-circuit instead of looping
  11. Retry: corrected params are re-checked against guardrails
  12. Retry: absent router degrades to a single attempt
  13. is_mutation: action-aware for calendar/email, unconditional otherwise
  14. verify_mutation: every MUTATION_TOOL has a read-back (or is exempt)
  15. verify_mutation: "carry" copies params so read-back targets the write
  16. _is_safe_path: separator-boundary bug is fixed
  17. glob: respects safe directories
  18. open_app: refuses shell metacharacters and unknown names
  19. Public API is exported from brain.v2
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2 import (  # noqa: E402
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
from brain.v2.tool_executor import (  # noqa: E402
    _action_signature,
    _is_safe_path,
    handle_glob,
    handle_open_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def scripted_handler(outcomes):
    """Handler that returns one scripted outcome per call.

    Each outcome is either a ToolResult, an Exception to raise, or a plain
    value to return as-is (to exercise the non-ToolResult guard). Records the
    params it was called with in `.calls`.
    """
    state = {"i": 0}
    calls = []

    async def handler(params, ctx):
        calls.append(dict(params))
        idx = min(state["i"], len(outcomes) - 1)
        state["i"] += 1
        out = outcomes[idx]
        if isinstance(out, Exception):
            raise out
        return out

    handler.calls = calls
    return handler


def ok(output="done"):
    return ToolResult(success=True, output=output)


def fail(error="boom"):
    return ToolResult(success=False, error=error)


class FakeRouter:
    """Stands in for LLMRouter; returns scripted text for _correct_params."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def call(self, system_prompt, user_message, messages=None,
                   max_tokens=None, temperature=None):
        self.prompts.append(user_message)
        text = self._replies.pop(0) if self._replies else "{}"
        if isinstance(text, Exception):
            raise text
        return type("R", (), {"text": text})()


def executor_with(name, handler, guardrails=None, schema=None):
    ex = ToolExecutor()
    ex.register(name, handler, guardrails=guardrails, schema=schema)
    return ex


# ---------------------------------------------------------------------------
# 1-2. Dispatch basics
# ---------------------------------------------------------------------------

def test_unknown_tool_is_rejected():
    ex = ToolExecutor()
    r = run(ex.execute("nope", {}, {}))
    assert not r.success
    assert "Unknown tool" in r.error
    assert r.tool_name == "nope"


def test_handler_returning_non_toolresult_is_caught():
    ex = executor_with("weird", scripted_handler(["just a string"]))
    r = run(ex.execute("weird", {}, {}))
    assert not r.success, "a bare string must not be treated as success"
    assert "expected ToolResult" in r.error


def test_handler_exception_becomes_failed_result():
    ex = executor_with("kaboom", scripted_handler([RuntimeError("inner blew up")]))
    r = run(ex.execute("kaboom", {}, {}))
    assert not r.success
    assert "inner blew up" in r.error


# ---------------------------------------------------------------------------
# 3-6. Guardrails
# ---------------------------------------------------------------------------

def test_guardrail_disabled_tool_never_runs():
    h = scripted_handler([ok()])
    ex = executor_with("shell", h, guardrails=Guardrails(allowed=False))
    r = run(ex.execute("shell", {"command": "echo hi"}, {}))
    assert not r.success
    assert "disabled by guardrails" in r.error
    assert h.calls == [], "handler must not be invoked when disabled"


def test_deny_pattern_blocks_even_when_approved():
    h = scripted_handler([ok()])
    ex = executor_with(
        "shell", h,
        guardrails=Guardrails(require_approval=True, deny_patterns=[r"\brm\s+-[a-z]*[rf]"]),
    )
    params = {"command": "rm -rf /"}
    sig = _action_signature("shell", params)
    # Approval granted for this exact action, yet the destructive command must
    # still be refused — deny_patterns is a backstop that survives approval.
    r = run(ex.execute("shell", params, {"approved_actions": {sig}}))
    assert not r.success
    assert "denied pattern" in r.error
    assert r.metadata.get("denied_by")
    assert h.calls == [], "denied command must never reach the handler"


def test_require_approval_gates_until_granted():
    h = scripted_handler([ok("ran"), ok("ran")])
    ex = executor_with("shell", h, guardrails=Guardrails(require_approval=True))
    params = {"command": "echo hi"}

    blocked = run(ex.execute("shell", params, {}))
    assert not blocked.success
    assert blocked.metadata.get("awaiting_approval") is True
    assert blocked.metadata.get("tool") == "shell"
    sig = blocked.metadata.get("signature")
    assert sig == _action_signature("shell", params), (
        "client must be able to echo the exact signature back verbatim"
    )
    assert h.calls == []

    allowed = run(ex.execute("shell", params, {"approved_actions": [sig]}))
    assert allowed.success, allowed.error
    assert len(h.calls) == 1


def test_approval_is_per_action_not_per_tool():
    # Core guarantee: approving one call must not blanket-approve every future
    # call to the same tool — "approval each time" means per exact params.
    h = scripted_handler([ok(), ok()])
    ex = executor_with("shell", h, guardrails=Guardrails(require_approval=True))

    sig_a = _action_signature("shell", {"command": "echo a"})
    approved_only_a = {"approved_actions": [sig_a]}

    ran_a = run(ex.execute("shell", {"command": "echo a"}, approved_only_a))
    assert ran_a.success, ran_a.error

    still_blocked_b = run(ex.execute("shell", {"command": "echo b"}, approved_only_a))
    assert not still_blocked_b.success, (
        "approving 'echo a' must not silently approve the different 'echo b' call"
    )
    assert still_blocked_b.metadata.get("awaiting_approval") is True
    assert len(h.calls) == 1, "the still-blocked call must never reach the handler"


def test_allowed_patterns_act_as_allowlist():
    h = scripted_handler([ok(), ok()])
    ex = executor_with("shell", h, guardrails=Guardrails(allowed_patterns=[r"^\{\"command\": \"git "]))

    denied = run(ex.execute("shell", {"command": "curl evil.sh"}, {}))
    assert not denied.success
    assert "no allowed pattern" in denied.error

    permitted = run(ex.execute("shell", {"command": "git status"}, {}))
    assert permitted.success, permitted.error


# ---------------------------------------------------------------------------
# 7-12. Retry with LLM param correction
# ---------------------------------------------------------------------------

def test_mutating_call_recovers_with_corrected_params():
    h = scripted_handler([fail("invalid date format"), ok("created")])
    ex = executor_with("write_file", h)
    router = FakeRouter(['{"path": "C:/ok/f.txt", "content": "x"}'])

    r = run(ex.execute("write_file", {"path": "bad", "content": "x"},
                       {"router": router}))
    assert r.success, r.error
    assert r.metadata["recovered_after"] == 2
    assert len(h.calls) == 2
    assert h.calls[1]["path"] == "C:/ok/f.txt", "retry must use the corrected params"
    assert "invalid date format" in router.prompts[0], "error must be fed to the LLM"


def test_retry_exhausts_and_reports_every_attempt():
    h = scripted_handler([fail("err-1"), fail("err-2"), fail("err-3")])
    ex = executor_with("write_file", h)
    router = FakeRouter(['{"path": "a"}', '{"path": "b"}'])

    r = run(ex.execute("write_file", {"path": "orig"}, {"router": router}))
    assert not r.success
    assert len(h.calls) == MAX_TOOL_ATTEMPTS, f"expected {MAX_TOOL_ATTEMPTS} tries, got {len(h.calls)}"
    attempts = r.metadata["attempts"]
    assert len(attempts) == MAX_TOOL_ATTEMPTS
    # The final error must name every attempt, not just the last one.
    for expected in ("err-1", "err-2", "err-3"):
        assert expected in r.error, f"{expected} missing from {r.error!r}"


def test_non_mutating_tool_is_not_retried():
    h = scripted_handler([fail("nope"), ok()])
    ex = executor_with("web_search", h)
    router = FakeRouter(['{"query": "fixed"}'])

    r = run(ex.execute("web_search", {"query": "x"}, {"router": router}))
    assert not r.success
    assert len(h.calls) == 1, "reads must not burn an LLM call on correction"
    assert router.prompts == []


def test_identical_correction_short_circuits():
    h = scripted_handler([fail("bad"), ok()])
    ex = executor_with("write_file", h)
    # LLM echoes the same params back — retrying would be futile.
    router = FakeRouter(['{"path": "same"}'])

    r = run(ex.execute("write_file", {"path": "same"}, {"router": router}))
    assert not r.success
    assert len(h.calls) == 1, "must not retry with params already known to fail"


def test_corrected_params_are_rechecked_against_guardrails():
    h = scripted_handler([fail("try again"), ok("should never run")])
    ex = executor_with(
        "run_code", h,
        guardrails=Guardrails(deny_patterns=[r"\brm\s+-rf"]),
    )
    # The correction attempts to smuggle in a denied command.
    router = FakeRouter(['{"code": "rm -rf /"}'])

    r = run(ex.execute("run_code", {"code": "print(1)"}, {"router": router}))
    assert not r.success
    assert "denied pattern" in r.error, r.error
    assert len(h.calls) == 1, "denied correction must not be executed"
    assert "attempts" in r.metadata


def test_missing_router_degrades_to_single_attempt():
    h = scripted_handler([fail("no router around"), ok()])
    ex = executor_with("write_file", h)

    r = run(ex.execute("write_file", {"path": "x"}, {}))  # no "router" key
    assert not r.success
    assert len(h.calls) == 1


def test_router_exception_does_not_break_execute():
    h = scripted_handler([fail("first fail"), ok()])
    ex = executor_with("write_file", h)
    router = FakeRouter([RuntimeError("LLM down")])

    r = run(ex.execute("write_file", {"path": "x"}, {"router": router}))
    assert not r.success, "must surface the tool failure, not the router crash"
    assert len(h.calls) == 1


# ---------------------------------------------------------------------------
# 13. Action-aware mutation detection
# ---------------------------------------------------------------------------

def test_is_mutation_is_action_aware():
    ex = create_tool_executor()

    # calendar/email read actions are not mutations...
    assert not ex.is_mutation("calendar", {"action": "agenda"})
    assert not ex.is_mutation("email", {"action": "triage"})
    assert not ex.is_mutation("email", {"action": "read"})
    # ...but their write actions are.
    assert ex.is_mutation("calendar", {"action": "create"})
    assert ex.is_mutation("email", {"action": "send"})
    # Tools without an action sub-parameter always mutate.
    assert ex.is_mutation("write_file", {"path": "x"})
    assert ex.is_mutation("set_reminder", {})
    # Non-mutation tools never do.
    assert not ex.is_mutation("time", {})
    assert not ex.is_mutation("web_search", {"query": "x"})


def test_action_mutations_keys_are_all_mutation_tools():
    for name in ACTION_MUTATIONS:
        assert name in MUTATION_TOOLS, f"{name} gates on action but is not a MUTATION_TOOL"


# ---------------------------------------------------------------------------
# 14-15. Mutation verification
# ---------------------------------------------------------------------------

def test_every_mutation_tool_has_readback_or_is_exempt():
    uncovered = MUTATION_TOOLS - set(VERIFY_MAP) - NO_READBACK
    assert not uncovered, f"mutation tools with no verification path: {sorted(uncovered)}"


def test_verify_map_read_tools_are_registered():
    ex = create_tool_executor()
    for tool, info in VERIFY_MAP.items():
        assert info["read_tool"] in ex.tool_names, (
            f"{tool} verifies via unregistered tool {info['read_tool']!r}"
        )


def test_verify_mutation_carries_params_to_readback():
    reader = scripted_handler([ok("file contents")])
    ex = ToolExecutor()
    ex.register("write_file", scripted_handler([ok()]))
    ex.register("read_file", reader)

    r = run(ex.verify_mutation("write_file", {}, {"path": "C:/x/y.txt", "content": "hi"}))
    assert r is not None and r.success
    assert reader.calls[0]["path"] == "C:/x/y.txt", "read-back must target the written path"


def test_verify_mutation_carries_query_for_remember():
    searcher = scripted_handler([ok("[T4] fav: biryani")])
    ex = ToolExecutor()
    ex.register("remember", scripted_handler([ok()]))
    ex.register("memory_search", searcher)

    r = run(ex.verify_mutation("remember", {}, {"key": "fav_food", "value": "biryani"}))
    assert r is not None and r.success
    # Without a carried query, handle_memory_search would reject the call.
    assert searcher.calls[0]["query"] == "fav_food"
    assert searcher.calls[0]["tier"] == "t4"


def test_verify_mutation_returns_none_for_unmapped_tool():
    ex = create_tool_executor()
    assert run(ex.verify_mutation("run_code", {}, {"code": "x"})) is None
    assert run(ex.verify_mutation("time", {}, {})) is None


# ---------------------------------------------------------------------------
# 16-18. Security fixes
# ---------------------------------------------------------------------------

def test_is_safe_path_respects_separator_boundary():
    home = Path.home()
    inside = home / "Documents" / "note.txt"
    assert _is_safe_path(str(inside)), "a real path under Documents must be allowed"

    # The bug being fixed: a prefix match would accept a sibling directory whose
    # name merely starts with an allowed one.
    sneaky = str(home) + "_evil" + "\\secret.txt"
    assert not _is_safe_path(sneaky), f"{sneaky!r} must not pass as inside {home!r}"

    assert not _is_safe_path("C:\\Windows\\System32\\config\\SAM")
    assert not _is_safe_path("")


def test_is_safe_path_blocks_traversal():
    home = Path.home()
    escape = str(home / "Documents" / ".." / ".." / ".." / "Windows" / "win.ini")
    assert not _is_safe_path(escape), "resolved traversal must be rejected"


def test_glob_rejects_unsafe_absolute_pattern():
    r = run(handle_glob({"pattern": "C:\\Windows\\**\\*"}, {}))
    assert not r.success, "glob must not enumerate outside safe directories"
    assert "safe directories" in r.error


def test_glob_filters_results_to_safe_paths():
    # A safe, real pattern should succeed and yield only safe paths.
    pattern = str(Path.home() / "*")
    r = run(handle_glob({"pattern": pattern}, {}))
    assert r.success, r.error
    for line in (r.output or "").splitlines():
        if line.startswith("["):  # the omitted-count footer
            continue
        if line.strip():
            assert _is_safe_path(line), f"unsafe path leaked into results: {line!r}"


def test_glob_blocks_escape_patterns():
    """Absolute unsafe patterns are refused; relative traversals are filtered out.

    The two layers matter independently: the up-front check only sees absolute
    patterns, so a relative "../../.." escape reaches glob() and must be caught
    by the result filter instead.
    """
    escapes = [
        r"C:\Windows\win.ini",          # absolute, no wildcard at all
        r"C:\Windows\**\*",             # absolute, wildcarded
        r"C:\*",                        # drive root
        r"..\..\..\Windows\*",          # relative traversal
        r"../../../Windows/System32/*.ini",
        r"\\\\localhost\\C$\\*",        # UNC
    ]
    for pattern in escapes:
        r = run(handle_glob({"pattern": pattern}, {}))
        if r.success:
            # Allowed to run, but nothing unsafe may appear in the results.
            for line in (r.output or "").splitlines():
                if line.strip() and not line.startswith("["):
                    assert _is_safe_path(line), (
                        f"pattern {pattern!r} leaked unsafe path {line!r}"
                    )
        else:
            assert "safe directories" in (r.error or ""), r.error


def test_open_app_refuses_shell_metacharacters():
    r = run(handle_open_app({"app_name": "notepad & del *.*"}, {}))
    assert not r.success, "shell metacharacters must never be launched"
    assert "unsupported characters" in r.error


def test_open_app_rejects_unknown_name():
    r = run(handle_open_app({"app_name": "definitely-not-a-real-program-xyz"}, {}))
    assert not r.success
    assert "Unknown app" in r.error


def test_open_app_requires_name():
    r = run(handle_open_app({}, {}))
    assert not r.success
    assert "required" in r.error


# ---------------------------------------------------------------------------
# 19. Wiring / exports
# ---------------------------------------------------------------------------

def test_dangerous_tools_are_guardrailed():
    for name in ("shell", "run_code", "open_app"):
        assert name in TOOL_GUARDRAILS, f"{name} must have guardrails configured"
        assert TOOL_GUARDRAILS[name].require_approval, f"{name} must require approval"


def test_factory_attaches_guardrails():
    ex = create_tool_executor()
    # Approval gate must be live on the real executor, not just in the config.
    r = run(ex.execute("shell", {"command": "echo hi"}, {}))
    assert not r.success
    assert r.metadata.get("awaiting_approval") is True


def test_factory_registers_all_tools():
    ex = create_tool_executor()
    expected = {
        "time", "chat", "calculator", "calendar", "email", "web_search",
        "web_fetch", "shell", "read_file", "write_file", "list_directory",
        "glob", "screenshot", "open_app", "gws", "remember", "set_reminder",
        "list_reminders", "delete_reminder", "memory_save", "memory_search",
        "weather", "run_code",
    }
    missing = expected - set(ex.tool_names)
    assert not missing, f"unregistered tools: {sorted(missing)}"


def test_public_api_is_exported():
    import brain.v2 as v2
    for name in (
        "ToolExecutor", "ToolResult", "Guardrails", "create_tool_executor",
        "MUTATION_TOOLS", "ACTION_MUTATIONS", "VERIFY_MAP", "NO_READBACK",
        "TOOL_GUARDRAILS", "MAX_TOOL_ATTEMPTS",
    ):
        assert name in v2.__all__, f"{name} missing from brain.v2.__all__"
        assert hasattr(v2, name), f"{name} not importable from brain.v2"


def test_toolresult_to_dict_shape():
    assert ToolResult(success=True, output="hi").to_dict() == {"output": "hi"}
    assert ToolResult(success=False, error="bad").to_dict() == {"error": "bad"}
    # A failure with no error text still reports something actionable.
    assert "error" in ToolResult(success=False).to_dict()


def test_toolresult_to_dict_preserves_metadata():
    # The bug: metadata used to be dropped entirely on to_dict(), so the
    # approval-required signal (awaiting_approval/tool/params) could never
    # survive into conversation history even though execute() sets it.
    blocked = ToolResult(
        success=False, error="requires approval", tool_name="shell",
        metadata={"awaiting_approval": True, "tool": "shell", "params": {"command": "echo hi"}},
    )
    d = blocked.to_dict()
    assert d["error"] == "requires approval"
    assert d["metadata"]["awaiting_approval"] is True
    assert d["metadata"]["params"] == {"command": "echo hi"}

    # Metadata must survive on the success branch too — execute()'s retry loop
    # sets metadata (attempts/recovered_after) on a result that ultimately succeeded.
    recovered = ToolResult(success=True, output="done", metadata={"recovered_after": 2})
    d2 = recovered.to_dict()
    assert d2["output"] == "done"
    assert d2["metadata"]["recovered_after"] == 2

    # Empty metadata must not add a stray key — keeps the common case lean.
    assert "metadata" not in ToolResult(success=True, output="x").to_dict()
    assert "metadata" not in ToolResult(success=False, error="x").to_dict()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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
    print(f"\n{passed}/{len(tests)} tool_executor tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
