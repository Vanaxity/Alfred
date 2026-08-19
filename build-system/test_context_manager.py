"""
ContextManager / ConversationHistory unit tests — Day 3 (compression).

Run directly:
    python build-system/test_context_manager.py

Covers:
  1. Message dataclass fields (role, content, token_count, metadata)
  2. add_user / add_assistant append and token_usage tracking
  3. add_tool_result JSON-serializes dicts, stringifies others, tags tool name
  4. to_llm_messages() returns LLM-ready role/content dicts
  5. Compression preserves the most recent tool-call + tool-result pair
  6. Old messages are collapsed, not lost piecemeal
  7. Public API exported from brain.v2 (ConversationHistory, Message)
"""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2 import (  # noqa: E402
    ConversationHistory,
    Message,
    count_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def no_tiktoken():
    """Force count_tokens to use the char heuristic, regardless of installs."""
    saved = sys.modules.get("tiktoken")
    sys.modules["tiktoken"] = None  # `import tiktoken` now raises ImportError
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("tiktoken", None)
        else:
            sys.modules["tiktoken"] = saved


def _tool_call(text: str) -> str:
    """Assistant message that reads as a tool call (contains 'tool' key)."""
    return json.dumps({"tool": text, "params": {}})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_message_dataclass_fields():
    msg = Message(role="user", content="hello", token_count=5)
    assert msg.role == "user" or msg.role.value == "user"
    assert msg.content == "hello"
    assert msg.token_count == 5
    assert isinstance(msg.metadata, dict)


def test_token_count_auto_computed():
    with no_tiktoken():
        msg = Message(role="user", content="a" * 100)
        assert msg.token_count == 25


def test_add_user_tracks_token_usage():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=100)
        ch.add_user("Hello")
        assert len(ch.messages) == 1
        assert ch.messages[0].role == "user"
        assert ch.token_usage == count_tokens("Hello")
        assert ch.token_usage == ch.total_tokens


def test_add_user_then_assistant_alternate():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=100)
        ch.add_user("Hi")
        ch.add_assistant("Hello there")
        roles = [m.role.value if hasattr(m.role, "value") else m.role for m in ch.messages]
        assert roles == ["user", "assistant"]


def test_add_tool_result_serializes_and_tags():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=200)
        ch.add_tool_result("get_time", {"hour": 12, "minute": 30})
        msg = ch.messages[-1]
        assert json.loads(msg.content) == {"hour": 12, "minute": 30}
        assert msg.metadata["tool"] == "get_time"
        assert msg.metadata["tool_name"] == "get_time"


def test_add_tool_result_stringifies_non_dict():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=200)
        ch.add_tool_result("shell", "hello output")
        assert ch.messages[-1].content == "hello output"


def test_to_llm_messages_shape():
    ch = ConversationHistory(token_budget=500)
    ch.add_user("hi")
    ch.add_assistant("hello")
    ch.add_tool_result("time", {"hour": 12})
    llm = ch.to_llm_messages()
    assert isinstance(llm, list) and len(llm) == 3
    assert all(set(m) == {"role", "content"} for m in llm)
    assert llm[0] == {"role": "user", "content": "hi"}
    assert llm[1] == {"role": "assistant", "content": "hello"}
    assert "time" in llm[2]["content"], "tool name surfaced in output"


def test_compress_preserves_latest_tool_pair():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=150)
        ch.add_user("U" * 400)
        ch.add_assistant(_tool_call("old"))
        ch.add_tool_result("old_tool", "R" * 400)
        ch.add_user("U" * 400)
        ch.add_assistant(_tool_call("new"))
        ch.add_tool_result("new_tool", "R" * 400)
        ch.add_assistant("A" * 400)

        assert ch.token_usage > 150, "budget was actually exceeded"

        contents = [m.content for m in ch.messages]
        roles = [m.role.value if hasattr(m.role, "value") else m.role for m in ch.messages]

        assert any("[Conversation compressed]" in c for c in contents), "summary marker present"
        assert _tool_call("new") in contents, "latest tool call preserved verbatim"
        assert "R" * 400 in contents, "latest tool result preserved verbatim"
        assert "A" * 400 in contents, "latest assistant reply preserved"
        assert not any("U" * 400 in c for c in contents), "old user turns collapsed"

        for a, b in zip(roles, roles[1:]):
            assert a != b, f"role alternation broken: {roles}"


def test_compress_noop_under_budget():
    with no_tiktoken():
        ch = ConversationHistory(token_budget=500)
        ch.add_user("hi")
        ch.add_assistant("hello")
        assert ch.compress_if_needed() == 0
        assert len(ch.messages) == 2


def test_reply_shaped_tool_call_is_treated_as_a_reply():
    """A reply the model dressed up as a tool call must not be dispatched.

    There is no `reply` tool. Seen live: the model emitted
    {"tool": "reply", "params": {...}}, the executor was handed a nonexistent
    tool, the turn was wasted, and the loop spun to MAX_TURNS.
    """
    from brain.v2.conversation import Alfred
    f = Alfred.__dict__["_reply_shaped_tool"].__func__

    assert f({"tool": "reply", "params": {"message": "42"}}) == "42"
    assert f({"tool": "respond", "params": {"text": "hi"}}) == "hi"
    assert f({"tool": "final_answer", "params": "plain string"}) == "plain string"
    assert f({"tool": "reply", "message": "top level"}) == "top level"

    # Real tool calls must pass straight through untouched.
    assert f({"tool": "calculator", "params": {"expression": "2+2"}}) is None
    assert f({"tool": "time", "params": {}}) is None


def test_tool_call_wins_over_narration_reply():
    """A tool call must beat a reply when the model emits both.

    The parser used to "prefer reply over tool call", which is backwards:
    models narrate before acting ({"reply": "Let me calculate that"} then
    {"tool": "calculator"}), and taking the narration meant the tool never ran
    and the narration became the answer -- a direct path to a confident
    ungrounded number.
    """
    from brain.v2.conversation import Alfred
    Stub = type("S", (), {
        "_loads_lenient": Alfred.__dict__["_loads_lenient"],
        "_parse_llm_output": Alfred.__dict__["_parse_llm_output"],
        "_reply_shaped_tool": Alfred.__dict__["_reply_shaped_tool"],
        "_extract_params": Alfred.__dict__["_extract_params"],
    })
    s = Stub()

    both = '{"reply": "Let me work that out."} {"tool": "calculator", "params": {"expression": "47*tan(radians(35))"}}'
    reply, tool, params = s._parse_llm_output(both)
    assert tool == "calculator", f"tool must win over narration, got reply={reply!r}"
    assert params == {"expression": "47*tan(radians(35))"}

    # Order in the text must not matter.
    reversed_order = '{"tool": "time", "params": {}} {"reply": "Checking the clock."}'
    reply, tool, params = s._parse_llm_output(reversed_order)
    assert tool == "time", f"tool must win regardless of order, got reply={reply!r}"

    # A lone reply is still a reply.
    reply, tool, params = s._parse_llm_output('{"reply": "The answer is 42."}')
    assert reply == "The answer is 42." and tool is None


def test_reply_is_not_truncated_at_500_chars():
    """Long explanations must survive. A hard [:500] cut silently amputated
    homework working mid-sentence."""
    from brain.v2.conversation import Alfred
    import json as _json
    Stub = type("S", (), {
        "_loads_lenient": Alfred.__dict__["_loads_lenient"],
        "_parse_llm_output": Alfred.__dict__["_parse_llm_output"],
        "_reply_shaped_tool": Alfred.__dict__["_reply_shaped_tool"],
        "_extract_params": Alfred.__dict__["_extract_params"],
    })
    long_answer = "Step one. " * 120  # ~1200 chars
    raw = _json.dumps({"reply": long_answer})
    reply, tool, params = Stub()._parse_llm_output(raw)
    assert reply is not None
    assert len(reply) > 500, f"reply was truncated to {len(reply)} chars"


def test_public_api_exported():
    assert ConversationHistory is not None
    assert Message is not None


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
    print(f"\n{passed}/{len(tests)} context_manager tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
