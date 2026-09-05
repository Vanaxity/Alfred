"""
brain_api.auth unit tests -- Q2 security audit fix.

Run directly:
    python build-system/test_brain_api_auth.py

Deliberately imports only brain_api.auth, not brain_api.server or brain:
the rest of the app pulls in packages (faiss, sentence-transformers,
pywin32, ...) not installed in every environment this suite runs in
(confirmed missing in the cloud sandbox this fix was first drafted in).
auth.py is stdlib-only specifically so this stays true.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Set a known key before import -- auth.py reads ALFRED_API_KEY once at
# import time, so this must happen first.
os.environ["ALFRED_API_KEY"] = "test-key-do-not-use-in-real-env"

import brain_api.auth as auth_module  # noqa: E402


def test_correct_header_key_authorized():
    assert auth_module.is_authorized({"X-Alfred-Key": "test-key-do-not-use-in-real-env"})


def test_case_insensitive_header_name_and_scheme():
    assert auth_module.is_authorized({"x-alfred-key": "test-key-do-not-use-in-real-env"})
    assert auth_module.is_authorized({"Authorization": "bearer test-key-do-not-use-in-real-env"})


def test_correct_bearer_header_authorized():
    assert auth_module.is_authorized({"Authorization": "Bearer test-key-do-not-use-in-real-env"})


def test_wrong_header_key_rejected():
    assert not auth_module.is_authorized({"X-Alfred-Key": "wrong-key"})


def test_no_key_anywhere_is_rejected():
    assert not auth_module.is_authorized({})
    assert not auth_module.is_authorized({}, {})


def test_empty_or_whitespace_key_never_matches():
    # Only relevant if ALFRED_API_KEY were ever empty -- guards against a
    # misconfigured env making an empty presented key "match" an empty
    # real key.
    assert not auth_module.is_authorized({"X-Alfred-Key": ""})
    assert not auth_module.is_authorized({"X-Alfred-Key": "   "})


def test_query_param_fallback_for_websocket():
    # Browsers can't set custom headers on a WebSocket handshake, so /ws
    # needs the ?key= fallback specifically.
    assert auth_module.is_authorized({}, {"key": "test-key-do-not-use-in-real-env"})
    assert not auth_module.is_authorized({}, {"key": "wrong"})


def test_header_takes_priority_over_query_param():
    # If both are present and disagree, the header (harder for a URL/log
    # to accidentally leak) should be what's actually checked.
    assert auth_module.is_authorized(
        {"X-Alfred-Key": "test-key-do-not-use-in-real-env"},
        {"key": "wrong"},
    )


def test_public_paths_is_minimal_and_explicit():
    assert auth_module.is_public_path("/health")
    assert not auth_module.is_public_path("/chat")
    assert not auth_module.is_public_path("/api/command")
    assert not auth_module.is_public_path("/ws")
    assert not auth_module.is_public_path("/")


def test_missing_env_var_generates_a_temporary_key_not_open_access():
    """Fail secure: no ALFRED_API_KEY must mean 'a key nobody knows', not
    'no auth'. Reload the module fresh with the env var absent to check
    this without permanently mutating the already-imported instance."""
    import importlib
    saved = os.environ.pop("ALFRED_API_KEY", None)
    try:
        fresh = importlib.reload(auth_module)
        assert fresh.API_KEY  # a real, non-empty generated value
        assert len(fresh.API_KEY) >= 32, "generated key should be long, not guessable"
        assert not fresh.is_authorized({})  # still rejects no-key requests
    finally:
        if saved is not None:
            os.environ["ALFRED_API_KEY"] = saved
        importlib.reload(auth_module)  # restore the real test key for any later test


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
    print(f"\n{passed}/{len(tests)} brain_api.auth tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
