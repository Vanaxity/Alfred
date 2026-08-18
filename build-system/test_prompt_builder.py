"""
PromptBuilder unit tests — Day 2 (token budget trimming).

Run directly:
    python build-system/test_prompt_builder.py

Covers:
  1. count_tokens() falls back to len(text)//4 when tiktoken is unavailable
  2. ToolSchema.to_prompt_block() renders the spec markdown format
  3. assemble() orders sections by priority (Identity > Rules > Profile > Tools > Skills > Memory)
  4. Low-priority sections are truncated (not dropped) when they don't fit
  5. Sections are dropped when truncation can't meaningfully contribute
  6. Public API is exported from brain.v2
"""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.v2 import (  # noqa: E402
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

TRUNCATED = "\u2026[truncated]"  # ellipsis + [truncated]


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_count_tokens_falls_back_when_tiktoken_missing():
    with no_tiktoken():
        assert count_tokens("a" * 100) == 25
        assert count_tokens("") == 0


def test_tool_schema_to_prompt_block_matches_spec():
    params = {"type": "object", "required": ["expr"]}
    tool = ToolSchema(
        name="calculator",
        description="Performs arithmetic",
        parameters=params,
    )
    expected = (
        "calculator\n"
        "Performs arithmetic Parameters:\n"
        "\n"
        + json.dumps(params, indent=2)
    )
    assert tool.to_prompt_block() == expected


def test_assemble_orders_sections_by_priority():
    with no_tiktoken():
        builder = PromptBuilder(token_budget=100_000)
        result = builder.assemble(
            identity="ID-CORE",
            rules=["RULE-ONE", "RULE-TWO"],
            profile="PROFILE-DATA",
            tools=[ToolSchema(name="TOOL-CALC", description="d", parameters={"type": "object"})],
            skills=[{"title": "SKILL-TITLE", "description": "SKILL-DESC", "steps": ["s1"]}],
            memory=["MEMORY-SNIPPET"],
        )
        text = result.system
        assert text.index("ID-CORE") < text.index("RULE-ONE"), "identity precedes rules"
        assert text.index("RULE-ONE") < text.index("PROFILE-DATA"), "rules precede profile"
        assert text.index("PROFILE-DATA") < text.index("TOOL-CALC"), "profile precedes tools"
        assert text.index("TOOL-CALC") < text.index("MEMORY-SNIPPET"), "tools precede memory"
        assert text.index("MEMORY-SNIPPET") < text.index("SKILL-TITLE"), (
            "memory precedes skills -- T3 episodic injection is a core Phase 1 "
            "ask and ranks above the single-skill guidance section"
        )
        assert isinstance(result, AssembledPrompt)
        assert result.tool_schemas == [ToolSchema(name="TOOL-CALC", description="d", parameters={"type": "object"})]
        assert result.memory_snippets == ["MEMORY-SNIPPET"]
        assert result.token_count > 0


def test_low_priority_section_is_truncated_not_dropped():
    with no_tiktoken():
        builder = PromptBuilder(token_budget=200)
        memory_big = "M" * 4000  # ~1000 tokens, far over budget
        result = builder.assemble(
            identity="I" * 40,  # ~10 tokens, fits
            memory=[memory_big],
        )
        assert "I" * 40 in result.system, "identity kept"
        assert TRUNCATED in result.system, "memory truncated with marker"
        assert memory_big not in result.system, "full memory not present"


def test_section_dropped_when_remaining_budget_too_small():
    with no_tiktoken():
        builder = PromptBuilder(token_budget=60)
        result = builder.assemble(
            identity="I" * 200,  # ~50 tokens, consumes nearly all budget
            memory=["M" * 1000],
        )
        assert "I" * 200 in result.system
        assert "M" not in result.system, "memory dropped when truncation isn't worthwhile"
        assert "memory" in result.dropped_sections, (
            "a full drop must be reported, not just silently absent from "
            "the text -- this is what lets a caller log that it happened"
        )


def test_dropped_sections_empty_when_everything_fits():
    with no_tiktoken():
        builder = PromptBuilder(token_budget=100_000)
        result = builder.assemble(identity="I", memory=["M"])
        assert result.dropped_sections == []


def test_public_api_exported():
    assert callable(count_tokens)
    assert PromptBuilder is not None
    assert ToolSchema is not None
    assert AssembledPrompt is not None
    assert PRIO_IDENTITY == 1 and PRIO_RULES == 2 and PRIO_PROFILE == 3
    assert PRIO_TOOLS == 4 and PRIO_MEMORY == 5 and PRIO_SKILLS == 6


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
    print(f"\n{passed}/{len(tests)} prompt_builder tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
