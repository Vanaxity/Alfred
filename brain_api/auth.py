"""
Alfred Brain API - shared-secret authentication.

Q2 in ROADMAP.md flagged a real gap, confirmed by direct code inspection:
every route in brain_api/server.py had no auth check at all, not just
/chat and /api/command. With the ngrok tunnel auto-starting on boot
(this week's Q6 work), anyone who obtains the tunnel URL could currently
read memory, trigger tools, or send email as Sam. This module is the fix:
every request must present a shared secret, checked here rather than
scattered per-route.

Stdlib-only on purpose (no fastapi/starlette imports) so the mocked unit
suite can import and test it without pulling in the rest of `brain_api`
or `brain` (faiss, sentence-transformers, pywin32, ...) -- keeps this
testable in any environment, including ones missing those heavy deps.
"""

import os
import secrets
from typing import Mapping, Optional


# Paths that must stay reachable with no key -- deliberately minimal.
# /health exposes only process status (uptime, memory-tier counts), not
# user data or the ability to act, and monitoring/uptime checks need it
# reachable unauthenticated.
PUBLIC_PATHS = frozenset({"/health"})

_env_key = os.environ.get("ALFRED_API_KEY", "").strip()
if _env_key:
    API_KEY = _env_key
else:
    # Fail secure, not open: a missing key must not mean "no auth", it
    # means "a key nobody else knows". Printed once at import time so it's
    # impossible to miss in the startup log, same convention as other
    # loud [SECURITY]-tagged warnings in this codebase.
    API_KEY = secrets.token_urlsafe(32)
    print(
        "\n" + "=" * 60 +
        "\n[SECURITY] ALFRED_API_KEY is not set. Generated a temporary "
        f"key for this run:\n  {API_KEY}\n"
        "Set ALFRED_API_KEY in your .env to use a stable key across "
        "restarts -- otherwise every client needs re-pointing whenever "
        "the server restarts.\n" + "=" * 60 + "\n"
    )


def _extract_key(headers: Mapping[str, str], query_params: Optional[Mapping[str, str]] = None) -> str:
    """Pull the presented key from wherever a client can plausibly put it:
    X-Alfred-Key header, Authorization: Bearer <key>, or (WebSocket clients
    can't always set custom headers) a ?key= query param."""
    # Header lookups must be case-insensitive -- HTTP header names are,
    # and Starlette's own Headers mapping already folds case, but the
    # plain dict a unit test passes in might not.
    lower = {k.lower(): v for k, v in headers.items()}
    direct = lower.get("x-alfred-key", "").strip()
    if direct:
        return direct
    auth = lower.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if query_params:
        return (query_params.get("key") or "").strip()
    return ""


def is_authorized(headers: Mapping[str, str], query_params: Optional[Mapping[str, str]] = None) -> bool:
    """True iff the request presents the correct shared secret. Uses a
    constant-time comparison -- a naive `==` on secrets leaks timing
    information about how many leading characters matched."""
    presented = _extract_key(headers, query_params)
    if not presented:
        return False
    return secrets.compare_digest(presented, API_KEY)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS
