"""
SkillManager tests — validate_skill() sandbox gate + SemVer versioning
(Phase 3 "Skill Validation & Versioning", ROADMAP.md Week 3 / PROJECT_TRACKER
items 23-24).

Run directly:
    python build-system/test_skill_validation.py

Covers:
  1. validate_skill() rejects an unknown tool.
  2. validate_skill() rejects a step missing a required param (web_search
     needs `query`).
  3. validate_skill() is action-aware for email/calendar (send needs `to`,
     triage/agenda don't need anything).
  4. validate_skill() actually dry-runs a calculator expression and catches
     a real failure (bad syntax), while accepting a valid one (incl. a
     function from the allowlist).
  5. generate_skill() routes a validation failure to drafts/ instead of the
     live T2 folder, and does NOT add it to the in-memory cache (so
     find_skill() could never match it).
  6. generate_skill() still saves a valid skill live, as before.
  7. list_draft_skills() / promote_draft_skill() round-trip a draft into
     the live cache and live folder.
  8. Skill version defaults to 0.1.0, round-trips through to_markdown() /
     from_markdown(), and improve_skill() bumps the patch version.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import brain.memory.skill_manager as skill_manager  # noqa: E402
from brain.memory.skill_manager import SkillManager, Skill  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_manager():
    """A SkillManager with no disk/memory-singleton setup -- __init__ is
    never run, so this touches no files and doesn't pull in get_memory()'s
    embedding model, matching test_skill_manager.py's pattern."""
    mgr = object.__new__(SkillManager)
    mgr._skills_cache = {}
    return mgr


def _skill(steps, skill_id="test-skill-01", **kwargs):
    return Skill(
        skill_id=skill_id,
        title=kwargs.pop("title", "Test Skill"),
        description=kwargs.pop("description", "A skill used only by this test."),
        steps=steps,
        tags=kwargs.pop("tags", ["test"]),
        complexity=kwargs.pop("complexity", "moderate"),
        path=kwargs.pop("path", ""),
        **kwargs,
    )


class _tmp_skills_dir:
    """Redirects the module-level T2_SKILLS_DIR global (generate_skill(),
    _save_as_draft(), list_draft_skills(), promote_draft_skill() all read
    it directly, not via self) to a throwaway directory for the duration
    of the `with` block."""

    def __enter__(self):
        self._old = skill_manager.T2_SKILLS_DIR
        self.path = Path(tempfile.mkdtemp(prefix="alfred_test_skills_"))
        skill_manager.T2_SKILLS_DIR = self.path
        return self.path

    def __exit__(self, *exc):
        skill_manager.T2_SKILLS_DIR = self._old
        shutil.rmtree(self.path, ignore_errors=True)


# ---------------------------------------------------------------------------
# validate_skill() — structural checks
# ---------------------------------------------------------------------------

def test_validate_skill_rejects_unknown_tool():
    mgr = _bare_manager()
    skill = _skill([{"tool": "nuke_the_datacenter", "description": "x", "params": {}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False
    assert any("unknown tool" in r for r in result.reasons)


def test_validate_skill_rejects_missing_required_param():
    mgr = _bare_manager()
    skill = _skill([{"tool": "web_search", "description": "search", "params": {}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False
    assert any("query" in r for r in result.reasons)


def test_validate_skill_accepts_well_formed_read_only_step():
    mgr = _bare_manager()
    skill = _skill([{"tool": "web_search", "description": "search", "params": {"query": "alfred ai"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is True
    assert result.reasons == []


def test_validate_skill_rejects_empty_steps():
    mgr = _bare_manager()
    skill = _skill([])
    result = mgr.validate_skill(skill)
    assert result.valid is False


# ---------------------------------------------------------------------------
# validate_skill() — action-conditional tools (email/calendar)
# ---------------------------------------------------------------------------

def test_validate_skill_email_send_needs_to():
    mgr = _bare_manager()
    skill = _skill([{"tool": "email", "description": "send", "params": {"action": "send", "subject": "hi"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False
    assert any("send" in r for r in result.reasons)


def test_validate_skill_email_triage_needs_nothing():
    mgr = _bare_manager()
    skill = _skill([{"tool": "email", "description": "triage", "params": {}}])
    result = mgr.validate_skill(skill)
    assert result.valid is True


def test_validate_skill_calendar_create_needs_summary():
    mgr = _bare_manager()
    skill = _skill([{"tool": "calendar", "description": "create", "params": {"action": "create"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False
    assert any("create" in r for r in result.reasons)


def test_validate_skill_calendar_agenda_needs_nothing():
    mgr = _bare_manager()
    skill = _skill([{"tool": "calendar", "description": "agenda", "params": {"action": "agenda"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is True


# ---------------------------------------------------------------------------
# validate_skill() — calculator dry run
# ---------------------------------------------------------------------------

def test_validate_skill_dry_runs_calculator_and_catches_bad_expression():
    mgr = _bare_manager()
    skill = _skill([{"tool": "calculator", "description": "compute", "params": {"expression": "2 +* 2"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False
    assert any("calculator dry run failed" in r for r in result.reasons)


def test_validate_skill_dry_runs_calculator_accepts_valid_expression():
    mgr = _bare_manager()
    skill = _skill([{"tool": "calculator", "description": "compute", "params": {"expression": "sqrt(16) + 2"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is True


def test_validate_skill_dry_runs_calculator_rejects_unsafe_expression():
    mgr = _bare_manager()
    skill = _skill([{"tool": "calculator", "description": "compute", "params": {"expression": "__import__('os').system('echo pwned')"}}])
    result = mgr.validate_skill(skill)
    assert result.valid is False


# ---------------------------------------------------------------------------
# generate_skill() wiring: drafts vs. live
# ---------------------------------------------------------------------------

def test_generate_skill_invalid_goes_to_drafts_not_live_cache():
    mgr = _bare_manager()
    with _tmp_skills_dir() as skills_dir:
        result = mgr.generate_skill(
            task="search for something specific",
            steps=[{"tool": "web_search", "description": "search", "params": {}}],  # missing query
            task_complexity="simple",
            had_error=False,
        )
        assert result is None
        assert mgr._skills_cache == {}
        assert not list(skills_dir.glob("*.md"))  # nothing landed in the live folder
        drafts = list((skills_dir / "drafts").glob("*.md"))
        assert len(drafts) == 1
        assert "Validation Failed" in drafts[0].read_text(encoding="utf-8")


def test_generate_skill_valid_still_saves_live():
    mgr = _bare_manager()
    with _tmp_skills_dir() as skills_dir:
        result = mgr.generate_skill(
            task="search for something specific",
            steps=[{"tool": "web_search", "description": "search", "params": {"query": "alfred ai project"}}],
            task_complexity="simple",
            had_error=False,
        )
        assert result is not None
        assert result.skill_id in mgr._skills_cache
        assert list(skills_dir.glob("*.md"))  # landed in the live folder
        assert not (skills_dir / "drafts").exists() or not list((skills_dir / "drafts").glob("*.md"))


# ---------------------------------------------------------------------------
# drafts round-trip: list_draft_skills() / promote_draft_skill()
# ---------------------------------------------------------------------------

def test_promote_draft_skill_moves_it_into_live_cache_and_folder():
    mgr = _bare_manager()
    with _tmp_skills_dir() as skills_dir:
        skill = _skill(
            [{"tool": "web_search", "description": "search", "params": {}}],
            skill_id="draft-abc123",
            title="Draft Me",
        )
        mgr._save_as_draft(skill, ["step 1: 'web_search' missing required params ['query']"])

        drafts = mgr.list_draft_skills()
        assert len(drafts) == 1
        assert drafts[0].skill_id == "draft-abc123"

        ok = mgr.promote_draft_skill("draft-abc123")
        assert ok is True
        assert "draft-abc123" in mgr._skills_cache
        assert list(skills_dir.glob("*.md"))  # moved into the live folder
        assert not list((skills_dir / "drafts").glob("*.md"))  # no longer in drafts


def test_promote_draft_skill_unknown_id_returns_false():
    mgr = _bare_manager()
    with _tmp_skills_dir():
        assert mgr.promote_draft_skill("does-not-exist") is False


def test_list_draft_skills_empty_when_no_drafts_dir():
    mgr = _bare_manager()
    with _tmp_skills_dir():
        assert mgr.list_draft_skills() == []


# ---------------------------------------------------------------------------
# SemVer versioning
# ---------------------------------------------------------------------------

def test_skill_defaults_to_version_0_1_0():
    skill = _skill([{"tool": "chat", "description": "x", "params": {}}])
    assert skill.version == "0.1.0"


def test_version_round_trips_through_markdown():
    skill = _skill([{"tool": "chat", "description": "x", "params": {}}], version="1.2.3")
    reloaded = Skill.from_markdown("irrelevant.md", skill.to_markdown())
    assert reloaded.version == "1.2.3"


def test_improve_skill_bumps_patch_version():
    mgr = _bare_manager()
    fd, path = tempfile.mkstemp(suffix=".md", prefix="alfred_test_skill_")
    os.close(fd)
    try:
        skill = _skill([{"tool": "chat", "description": "x", "params": {}}], path=path)
        assert skill.version == "0.1.0"
        mgr._skills_cache[skill.skill_id] = skill

        mgr.improve_skill(skill.skill_id, "first fix")
        assert skill.version == "0.1.1"

        mgr.improve_skill(skill.skill_id, "second fix")
        assert skill.version == "0.1.2"

        on_disk = Path(path).read_text(encoding="utf-8")
        assert "**Version:** 0.1.2" in on_disk
    finally:
        os.unlink(path)


def test_bump_patch_version_handles_malformed_version():
    assert skill_manager._bump_patch_version("not-a-version") == "0.1.1"
    assert skill_manager._bump_patch_version("1.2.3") == "1.2.4"


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
    print(f"\n{passed}/{len(tests)} skill_validation tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
