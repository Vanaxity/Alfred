"""
Entity graph tests — ROADMAP.md Phase 3, "Entity graph & synthesis
(GBrain-inspired) -- the actual 'grows with you' mechanism."

Covers three layers:
  1. brain/memory/entity_graph.py's EntityGraph against a real temp-file
     sqlite db (upsert/dedupe, relations, synthesis, note-cap enforcement).
  2. The three new tool handlers (entity_note/entity_relate/entity_lookup)
     through the real ToolExecutor, including the "entity graph unavailable"
     path a bare/partial ctx can hit.
  3. Wiring into Alfred: the new tools reach _get_tool_descriptions() (or
     the curator's prompt-building would KeyError), the post-turn curation
     pass can actually call entity_note end-to-end, and execute() doesn't
     break when self.entity_graph was never set (mirrors the existing
     Alfred.__new__() bare-instance test pattern this project already uses
     for _mcp_tool_schemas).

Run directly:
    python build-system/test_entity_graph.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.memory.entity_graph import EntityGraph  # noqa: E402
from brain.v2.tool_executor import (  # noqa: E402
    MUTATION_TOOLS,
    ToolResult,
    create_tool_executor,
    handle_entity_lookup,
    handle_entity_note,
    handle_entity_relate,
)
from brain.v2.conversation import Alfred  # noqa: E402
from brain.v2.prompt_builder import PromptBuilder  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _temp_graph() -> EntityGraph:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # EntityGraph creates it fresh
    return EntityGraph(db_path=Path(path))


def _cleanup(graph: EntityGraph) -> None:
    graph._conn.close()
    try:
        os.unlink(graph.db_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 1. EntityGraph storage
# ---------------------------------------------------------------------------

def test_upsert_entity_creates_new():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Priya", "person", "mentioned re: robotics club")
        entity = graph.get_entity("Priya")
        assert entity is not None
        assert entity["name"] == "Priya"
        assert entity["entity_type"] == "person"
        assert entity["mention_count"] == 1
        assert entity["notes"] == ["mentioned re: robotics club"]
    finally:
        _cleanup(graph)


def test_upsert_entity_dedupes_case_and_whitespace():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Priya Sharma", "person")
        graph.upsert_entity("  priya   sharma  ", "person", "second mention")
        entities = graph.list_entities()
        assert len(entities) == 1
        assert entities[0]["mention_count"] == 2
        assert entities[0]["notes"] == ["second mention"]
    finally:
        _cleanup(graph)


def test_upsert_entity_does_not_overwrite_known_type_with_unknown():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Acme Corp", "organization")
        graph.upsert_entity("Acme Corp")  # default entity_type="unknown"
        entity = graph.get_entity("Acme Corp")
        assert entity["entity_type"] == "organization"
    finally:
        _cleanup(graph)


def test_upsert_entity_rejects_empty_name():
    graph = _temp_graph()
    try:
        try:
            graph.upsert_entity("   ")
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        _cleanup(graph)


def test_upsert_entity_caps_notes():
    graph = _temp_graph()
    try:
        for i in range(25):
            graph.upsert_entity("Priya", "person", f"note {i}")
        entity = graph.get_entity("Priya")
        assert len(entity["notes"]) == 20
        assert entity["notes"][-1] == "note 24"
        assert entity["notes"][0] == "note 5"
    finally:
        _cleanup(graph)


def test_add_relation_creates_missing_entities():
    graph = _temp_graph()
    try:
        graph.add_relation("Priya", "works on", "Robotics Club")
        assert graph.get_entity("Priya") is not None
        assert graph.get_entity("Robotics Club") is not None
        relations = graph.get_relations_for("Priya")
        assert len(relations) == 1
        assert relations[0]["entity_a"] == "Priya"
        assert relations[0]["relation"] == "works on"
        assert relations[0]["entity_b"] == "Robotics Club"
    finally:
        _cleanup(graph)


def test_add_relation_repeat_bumps_count_not_duplicate():
    graph = _temp_graph()
    try:
        graph.add_relation("Priya", "works on", "Robotics Club")
        graph.add_relation("priya", "WORKS ON", "robotics club")
        relations = graph.get_relations_for("Priya")
        assert len(relations) == 1
        assert relations[0]["mention_count"] == 2
    finally:
        _cleanup(graph)


def test_add_relation_rejects_missing_parts():
    graph = _temp_graph()
    try:
        for bad in [("", "rel", "b"), ("a", "", "b"), ("a", "rel", "")]:
            try:
                graph.add_relation(*bad)
                assert False, f"expected ValueError for {bad}"
            except ValueError:
                pass
    finally:
        _cleanup(graph)


def test_get_relations_for_finds_entity_on_either_side():
    graph = _temp_graph()
    try:
        graph.add_relation("Priya", "manages", "Alex")
        assert len(graph.get_relations_for("Priya")) == 1
        assert len(graph.get_relations_for("Alex")) == 1
        assert len(graph.get_relations_for("Nobody")) == 0
    finally:
        _cleanup(graph)


def test_get_entity_missing_returns_none():
    graph = _temp_graph()
    try:
        assert graph.get_entity("Nobody") is None
    finally:
        _cleanup(graph)


def test_list_entities_filters_by_type_and_limit():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Priya", "person")
        graph.upsert_entity("Alex", "person")
        graph.upsert_entity("Acme Corp", "organization")
        people = graph.list_entities(entity_type="person")
        assert {e["name"] for e in people} == {"Priya", "Alex"}
        assert len(graph.list_entities(limit=1)) == 1
    finally:
        _cleanup(graph)


def test_synthesize_missing_entity():
    graph = _temp_graph()
    try:
        text = graph.synthesize("Nobody")
        assert "No entity graph data" in text
    finally:
        _cleanup(graph)


def test_synthesize_includes_notes_and_relations():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Priya", "person", "leads the robotics team")
        graph.add_relation("Priya", "works on", "Robotics Club")
        text = graph.synthesize("Priya")
        assert "Priya (person)" in text
        assert "leads the robotics team" in text
        assert "Priya works on Robotics Club" in text
    finally:
        _cleanup(graph)


def test_name_containing_pipe_does_not_break_relation_lookup():
    graph = _temp_graph()
    try:
        graph.add_relation("A|B", "relates to", "C")
        # Sanitized in the key, so this must not raise or silently drop the row.
        relations = graph.get_relations_for("A|B")
        assert len(relations) == 1
    finally:
        _cleanup(graph)


# ---------------------------------------------------------------------------
# 2. Tool handlers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_handle_entity_note_success():
    graph = _temp_graph()
    try:
        result = _run(handle_entity_note(
            {"name": "Priya", "entity_type": "person", "note": "hi"},
            {"entity_graph": graph},
        ))
        assert result.success
        assert graph.get_entity("Priya") is not None
    finally:
        _cleanup(graph)


def test_handle_entity_note_requires_name():
    result = _run(handle_entity_note({}, {"entity_graph": _temp_graph()}))
    assert not result.success


def test_handle_entity_note_no_graph_in_ctx():
    result = _run(handle_entity_note({"name": "Priya"}, {}))
    assert not result.success
    assert "unavailable" in result.error.lower()


def test_handle_entity_relate_success():
    graph = _temp_graph()
    try:
        result = _run(handle_entity_relate(
            {"entity_a": "Priya", "relation": "manages", "entity_b": "Alex"},
            {"entity_graph": graph},
        ))
        assert result.success
        assert len(graph.get_relations_for("Priya")) == 1
    finally:
        _cleanup(graph)


def test_handle_entity_relate_requires_all_three():
    result = _run(handle_entity_relate(
        {"entity_a": "Priya", "relation": "manages"}, {"entity_graph": _temp_graph()}
    ))
    assert not result.success


def test_handle_entity_lookup_returns_synthesis():
    graph = _temp_graph()
    try:
        graph.upsert_entity("Priya", "person", "leads the robotics team")
        result = _run(handle_entity_lookup({"name": "Priya"}, {"entity_graph": graph}))
        assert result.success
        assert "leads the robotics team" in result.output
    finally:
        _cleanup(graph)


def test_handle_entity_lookup_no_graph_in_ctx():
    result = _run(handle_entity_lookup({"name": "Priya"}, {}))
    assert not result.success


# ---------------------------------------------------------------------------
# 3. Wiring into ToolExecutor and Alfred
# ---------------------------------------------------------------------------

def test_tool_executor_registers_entity_tools():
    executor = create_tool_executor()
    for name in ("entity_note", "entity_relate", "entity_lookup"):
        assert name in executor._handlers


def test_entity_note_and_relate_are_mutations_lookup_is_not():
    assert "entity_note" in MUTATION_TOOLS
    assert "entity_relate" in MUTATION_TOOLS
    assert "entity_lookup" not in MUTATION_TOOLS


def test_get_tool_descriptions_includes_entity_tools():
    alfred = Alfred.__new__(Alfred)
    descriptions = alfred._get_tool_descriptions()
    for name in ("entity_note", "entity_relate", "entity_lookup"):
        assert name in descriptions
        assert "description" in descriptions[name]
        assert "params" in descriptions[name]


class _LLMResponse:
    def __init__(self, text):
        self.text = text
        self.provider = "fake"
        self.fallback_used = False
        self.fallback_reason = None


class _FakeMemory:
    def get_context_for_llm(self, query=None):
        return ""

    def t3_find_episodes(self, query, max_results=2):
        return []

    def t3_save_episode(self, title, content):
        return "fake/path.md"


class _FakeExpanded:
    def __init__(self, expanded):
        self.expanded = expanded


class _FakeGoalExpander:
    async def expand(self, user_input):
        return _FakeExpanded(user_input)


class _FakeSkillManager:
    def find_skill(self, text, search_ecosystem=False):
        return None

    def generate_skill(self, **kwargs):
        return None

    def improve_skill(self, skill_id, note):
        return None


class _FakeRouter:
    """Returns queued responses in order, repeating the last once exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        text = self._responses.pop(0) if self._responses else '{"reply": "nothing to save"}'
        return _LLMResponse(text)


def _make_bare_alfred(router_responses, entity_graph=None):
    a = Alfred.__new__(Alfred)
    a.memory = _FakeMemory()
    a.skill_manager = _FakeSkillManager()
    a.goal_expander = _FakeGoalExpander()
    a.db = None
    a._router = _FakeRouter(router_responses)
    a._prompt_builder = PromptBuilder(token_budget=8000)
    a._tool_executor = create_tool_executor()
    a._bootstrap = {}
    a._pending_curation_tasks = []
    if entity_graph is not None:
        a.entity_graph = entity_graph
    return a


async def _drain_curation(alfred):
    for t in list(alfred._pending_curation_tasks):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            pass


async def _test_execute_survives_missing_entity_graph_attribute():
    """A bare Alfred that never got self.entity_graph set (the exact shape
    of the existing test_speed_audit_timing.py fixture, unmodified) must
    not AttributeError inside execute() -- the entity_graph tool_ctx entry
    has to fall back to None via getattr, not self.entity_graph directly."""
    alfred = _make_bare_alfred(['{"reply": "Hello there."}'])
    result = await alfred.execute("say hi", {})
    await _drain_curation(alfred)
    assert result["response"] == "Hello there."


async def _test_curation_pass_can_call_entity_note_end_to_end():
    graph = _temp_graph()
    try:
        alfred = _make_bare_alfred(
            [
                '{"reply": "Sure, I\'ll keep that in mind."}',
                '{"tool": "entity_note", "params": '
                '{"name": "Priya", "entity_type": "person", '
                '"note": "leads the robotics club"}}',
            ],
            entity_graph=graph,
        )
        await alfred.execute("Priya leads the robotics club now", {})
        await _drain_curation(alfred)
        entity = graph.get_entity("Priya")
        assert entity is not None
        assert entity["entity_type"] == "person"
        assert "leads the robotics club" in entity["notes"]
    finally:
        _cleanup(graph)


def test_execute_survives_missing_entity_graph_attribute():
    asyncio.run(_test_execute_survives_missing_entity_graph_attribute())


def test_curation_pass_can_call_entity_note_end_to_end():
    asyncio.run(_test_curation_pass_can_call_entity_note_end_to_end())


# ---------------------------------------------------------------------------
# Runner (mirrors the other build-system/test_*.py files: plain defs named
# test_*, run directly, no pytest).
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
    print(f"\n{passed}/{len(tests)} entity_graph tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
