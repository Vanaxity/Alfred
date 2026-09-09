"""
SkillManager unit tests — improve_skill() cache-consistency fix.

Run directly:
    python build-system/test_skill_manager.py

Covers:
  1. improve_skill() with new_steps updates the cached Skill object's
     .steps, not just the markdown written to disk (the confirmed bug:
     PROJECT_TRACKER.md #30/258 -- the next time this skill is matched and
     injected into a prompt, it used to still show the old steps because
     the cache was never touched).
  2. improve_skill() persists the cache update — self._skills_cache[id] is
     the same, updated object, not a stale copy.
  3. improve_skill() with an unknown skill_id returns False without raising
     and without creating a tempfile.
  4. improve_skill() without new_steps still logs the improvement note to
     disk and returns True, leaving .steps untouched.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.memory.skill_manager import SkillManager, Skill  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_manager():
    """A SkillManager with no disk/memory-singleton setup -- __init__ is
    never run, so this touches no files and doesn't pull in get_memory()'s
    embedding model. Only improve_skill()'s cache/steps logic is under test."""
    mgr = object.__new__(SkillManager)
    mgr._skills_cache = {}
    return mgr


def _temp_skill(skill_id="test-skill-01", steps=None):
    fd, path = tempfile.mkstemp(suffix=".md", prefix="alfred_test_skill_")
    os.close(fd)
    skill = Skill(
        skill_id=skill_id,
        title="Test Skill",
        description="A skill used only by this test.",
        steps=steps or [{"tool": "web_search", "description": "search", "params": {}}],
        tags=["test"],
        complexity="moderate",
        path=path,
    )
    return skill, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_improve_skill_updates_cached_steps():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        new_steps = [{"tool": "web_search", "description": "search harder", "params": {"retries": 2}}]
        ok = mgr.improve_skill(skill.skill_id, "step failed, added retry", new_steps=new_steps)
        assert ok is True
        assert mgr._skills_cache[skill.skill_id].steps == new_steps
    finally:
        os.unlink(path)


def test_improve_skill_persists_same_updated_object_in_cache():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.improve_skill(skill.skill_id, "note", new_steps=[{"tool": "chat", "description": "x", "params": {}}])
        # Not a stale copy — same identity, mutated in place and re-stored.
        assert mgr._skills_cache[skill.skill_id] is skill
        assert skill.steps == [{"tool": "chat", "description": "x", "params": {}}]
    finally:
        os.unlink(path)


def test_improve_skill_writes_updated_steps_to_disk():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.improve_skill(skill.skill_id, "note", new_steps=[{"tool": "chat", "description": "x", "params": {}}])
        on_disk = Path(path).read_text(encoding="utf-8")
        assert "Skill Improvement Log" in on_disk
        assert "`chat`" in on_disk  # regenerated from the updated Skill object
    finally:
        os.unlink(path)


def test_improve_skill_missing_skill_id_returns_false():
    mgr = _bare_manager()
    assert mgr.improve_skill("does-not-exist", "note") is False


def test_improve_skill_without_new_steps_leaves_steps_untouched():
    mgr = _bare_manager()
    original_steps = [{"tool": "web_search", "description": "search", "params": {}}]
    skill, path = _temp_skill(steps=original_steps)
    mgr._skills_cache[skill.skill_id] = skill
    try:
        ok = mgr.improve_skill(skill.skill_id, "just a note, no step change")
        assert ok is True
        assert mgr._skills_cache[skill.skill_id].steps == original_steps
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Markdown round-trip: success/failure counts and Tool Forge state
#
# to_markdown() always wrote a "Success Rate: X/Y" line, but from_markdown()
# never read it back -- every skill's success_count/failure_count silently
# reset to 0 on every SkillManager reload (server restart, or a fresh
# process picking up skills from disk). That's the exact counter Tool
# Forge's "used 3+ times" promotion threshold depends on, so a reload
# wiping it out made the threshold unreachable across restarts. Fixed
# alongside adding forged_tool/forge_attempts, which needed the same
# round-trip.
# ---------------------------------------------------------------------------

def test_skill_markdown_round_trip_persists_success_and_failure_counts():
    skill, path = _temp_skill()
    skill.success_count = 5
    skill.failure_count = 2
    try:
        reloaded = Skill.from_markdown(path, skill.to_markdown())
        assert reloaded.success_count == 5
        assert reloaded.failure_count == 2
    finally:
        os.unlink(path)


def test_skill_markdown_round_trip_persists_forge_state():
    skill, path = _temp_skill()
    skill.forged_tool = "forged_do_the_thing_abc12345"
    try:
        reloaded = Skill.from_markdown(path, skill.to_markdown())
        assert reloaded.forged_tool == "forged_do_the_thing_abc12345"
    finally:
        os.unlink(path)


def test_skill_markdown_round_trip_persists_forge_attempts_when_not_forged():
    skill, path = _temp_skill()
    skill.forge_attempts = 2
    try:
        reloaded = Skill.from_markdown(path, skill.to_markdown())
        assert reloaded.forge_attempts == 2
        assert reloaded.forged_tool is None
    finally:
        os.unlink(path)


def test_skill_markdown_round_trip_defaults_for_never_forged_fresh_skill():
    skill, path = _temp_skill()
    try:
        reloaded = Skill.from_markdown(path, skill.to_markdown())
        assert reloaded.forged_tool is None
        assert reloaded.forge_attempts == 0
        assert reloaded.success_count == 0
        assert reloaded.failure_count == 0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# record_skill_use / mark_forged / record_forge_attempt
# ---------------------------------------------------------------------------

def test_record_skill_use_increments_success_and_persists():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        ok = mgr.record_skill_use(skill.skill_id, success=True)
        assert ok is True
        assert mgr._skills_cache[skill.skill_id].success_count == 1
        assert mgr._skills_cache[skill.skill_id].failure_count == 0
        # Persisted, not just held in memory.
        reloaded = Skill.from_markdown(path, Path(path).read_text(encoding="utf-8"))
        assert reloaded.success_count == 1
    finally:
        os.unlink(path)


def test_record_skill_use_increments_failure():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.record_skill_use(skill.skill_id, success=False)
        assert mgr._skills_cache[skill.skill_id].failure_count == 1
        assert mgr._skills_cache[skill.skill_id].success_count == 0
    finally:
        os.unlink(path)


def test_record_skill_use_accumulates_across_calls():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.record_skill_use(skill.skill_id, success=True)
        mgr.record_skill_use(skill.skill_id, success=True)
        mgr.record_skill_use(skill.skill_id, success=False)
        cached = mgr._skills_cache[skill.skill_id]
        assert cached.success_count == 2
        assert cached.failure_count == 1
    finally:
        os.unlink(path)


def test_record_skill_use_missing_skill_id_returns_false():
    mgr = _bare_manager()
    assert mgr.record_skill_use("does-not-exist", success=True) is False


def test_mark_forged_sets_field_and_persists():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        ok = mgr.mark_forged(skill.skill_id, "forged_thing_abc123")
        assert ok is True
        assert mgr._skills_cache[skill.skill_id].forged_tool == "forged_thing_abc123"
        reloaded = Skill.from_markdown(path, Path(path).read_text(encoding="utf-8"))
        assert reloaded.forged_tool == "forged_thing_abc123"
    finally:
        os.unlink(path)


def test_record_forge_attempt_increments_and_persists():
    mgr = _bare_manager()
    skill, path = _temp_skill()
    mgr._skills_cache[skill.skill_id] = skill
    try:
        mgr.record_forge_attempt(skill.skill_id)
        mgr.record_forge_attempt(skill.skill_id)
        assert mgr._skills_cache[skill.skill_id].forge_attempts == 2
        reloaded = Skill.from_markdown(path, Path(path).read_text(encoding="utf-8"))
        assert reloaded.forge_attempts == 2
    finally:
        os.unlink(path)


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
    print(f"\n{passed}/{len(tests)} skill_manager tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
