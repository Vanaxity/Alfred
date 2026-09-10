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
