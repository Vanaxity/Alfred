"""
T3 episode-save tests -- Phase A item 5 follow-up (2026-09-10).

Found while manually testing the loop: asking "read your brain/v2 files
and explain the turn loop" produced a correct answer but saved NO T3
episode -- silently. Root cause: t3_save_episode() derives the filename
from the task text with only `title[:50].replace(' ', '-')`, so a task
containing '/' (here "brain/v2") yields "read-your-brain/v2-files-...md".
`T3_EPISODIC_DIR / that` then points at a non-existent subdirectory,
`write_text` raises FileNotFoundError, and execute()'s `except Exception:
pass` swallows it. Any task with '/' '\\' ':' etc. in the first 50 chars
loses its episode with no trace.

These tests run t3_save_episode against a temp directory (never the real
Obsidian vault) on a bare FiveTierMemory instance (no __init__, no
SentenceTransformer/faiss). Run directly:

    python build-system/test_t3_episode.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import brain.memory.five_tier as ft  # noqa: E402


def _bare_memory():
    """FiveTierMemory with no __init__ -- t3_save_episode only touches
    self._t3_vector_model / self._t3_faiss_index (skipped when both None)
    and self._t3_bm25_dirty (a plain attribute)."""
    m = ft.FiveTierMemory.__new__(ft.FiveTierMemory)
    m._t3_vector_model = None
    m._t3_faiss_index = None
    return m


def _with_tmp_episodic_dir(fn):
    tmp = Path(tempfile.mkdtemp(prefix="t3test_"))
    original = ft.T3_EPISODIC_DIR
    ft.T3_EPISODIC_DIR = tmp
    try:
        fn(tmp)
    finally:
        ft.T3_EPISODIC_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_title_with_a_slash_still_saves_a_flat_file():
    def body(tmp):
        m = _bare_memory()
        try:
            path = m.t3_save_episode(
                title="read your brain/v2 files and explain the loop",
                content="the body",
            )
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"t3_save_episode crashed on a title containing '/': {e!r}"
            )
        p = Path(path)
        assert p.exists(), f"episode file was not created: {path}"
        assert p.parent == tmp, f"file escaped the episodic dir: {p.parent} != {tmp}"
        assert "/" not in p.name and "\\" not in p.name, f"separator leaked into name: {p.name}"
        assert p.read_text(encoding="utf-8").strip(), "file is empty"

    _with_tmp_episodic_dir(body)


def test_other_reserved_characters_are_sanitised():
    def body(tmp):
        m = _bare_memory()
        # ':' '?' '*' '|' are all illegal in Windows filenames.
        path = m.t3_save_episode(
            title='what time is it? check C:\\Users and *everything*',
            content="x",
        )
        p = Path(path)
        assert p.exists()
        assert p.parent == tmp
        for bad in ':?*|\\/':
            assert bad not in p.name, f"{bad!r} not sanitised out of {p.name!r}"

    _with_tmp_episodic_dir(body)


def test_ordinary_title_is_still_readable_in_the_filename():
    def body(tmp):
        m = _bare_memory()
        path = m.t3_save_episode(title="Delete the physics study block", content="x")
        name = Path(path).name
        assert name.endswith(".md")
        assert "physics" in name.lower() and "block" in name.lower(), (
            f"a normal title should stay legible: {name}"
        )

    _with_tmp_episodic_dir(body)


def test_all_reserved_title_still_produces_a_valid_name():
    def body(tmp):
        m = _bare_memory()
        path = m.t3_save_episode(title="/// \\\\\\ ::: ", content="x")
        p = Path(path)
        assert p.exists(), "a title that is entirely reserved chars must still save"
        assert p.parent == tmp
        assert p.name.endswith(".md") and len(p.stem) > 3

    _with_tmp_episodic_dir(body)


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
    print(f"\n{passed}/{len(tests)} t3_episode tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
