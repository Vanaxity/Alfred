"""
LLMRouter unit tests — fail-fast provider failover.

Run directly:
    python build-system/test_llm_router.py

Covers:
  1. Timeout on provider A -> failover to B
  2. Rate limit (429) on A -> failover to B
  3. Circuit breaker opens after 2 failures, provider skipped
  4. Breaker recovers to HALF_OPEN after window, probe passes, real call proceeds
  5. Probe failure on HALF_OPEN re-opens breaker, next provider used
  6. get_stats() reflects observed counters
  7. Adaptive timeout shrinks on timeout, recovers toward base on success
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.llm_router import LLMRouter, Provider, CircuitBreaker  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, text):
        self.message = _Msg(text)


class _Resp:
    def __init__(self, text):
        self.choices = [_Choice(text)]


class FakeCompletions:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = []

    def create(self, *args, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior == "ok":
            return _Resp("hello")
        if behavior == "timeout":
            raise TimeoutError("timed out")
        if behavior == "429":
            raise Exception("Error code: 429 - rate limit exceeded")
        if behavior == "500":
            raise Exception("Error code: 500 - internal server error")
        raise AssertionError(f"unknown behavior: {behavior}")


class FakeChat:
    def __init__(self, behaviors):
        self.completions = FakeCompletions(behaviors)


class FakeClient:
    """OpenAI-compatible fake with scripted per-call behavior."""
    def __init__(self, behaviors):
        self.chat = FakeChat(behaviors)


def make_provider(name, behaviors, priority=1, base_timeout=10.0):
    return Provider(
        name=name,
        model="fake-model",
        client=FakeClient(behaviors),
        priority=priority,
        api_key="fake",
        base_timeout=base_timeout,
    )


def build_router(providers):
    router = LLMRouter(groq_key="", gemini_key="", openrouter_key="")
    router.providers = providers
    router.breakers = {p.name: CircuitBreaker() for p in providers}
    return router


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_timeout_fails_over():
    router = build_router([
        make_provider("a", ["timeout"]),
        make_provider("b", ["ok"]),
    ])
    resp = run(router.call("sys", "hello"))
    assert resp.text == "hello", f"expected b text, got {resp.text!r}"
    assert resp.provider == "b"
    assert resp.fallback_used is True
    assert resp.fallback_reason and "timeout" in resp.fallback_reason


def test_rate_limit_fails_over():
    router = build_router([
        make_provider("a", ["429"]),
        make_provider("b", ["ok"]),
    ])
    resp = run(router.call("sys", "hello"))
    assert resp.provider == "b"
    assert resp.fallback_used is True
    assert resp.fallback_reason and "429" in resp.fallback_reason


def test_circuit_breaker_opens_after_two_failures():
    router = build_router([
        make_provider("a", ["429", "429"]),
        make_provider("b", ["ok", "ok", "ok"]),
    ])
    for _ in range(2):
        resp = run(router.call("sys", "hi"))
        assert resp.provider == "b"
    assert router.breakers["a"].state.name == "OPEN"
    assert router.breakers["a"].failure_count == 2
    # Third call: breaker open, 'a' skipped without any attempt
    resp = run(router.call("sys", "hi"))
    assert resp.provider == "b"
    assert router.breakers["a"].failure_count == 2


def test_breaker_recovers_to_half_open_and_probes():
    import brain.llm_router as mod
    router = build_router([
        make_provider("a", ["429", "429", "ok"]),
        make_provider("b", ["ok"]),
    ])
    real_time = time.time
    now = [real_time()]
    mod.time.time = lambda: now[0]
    try:
        for _ in range(2):
            run(router.call("sys", "hi"))
        assert router.breakers["a"].state.name == "OPEN"
        now[0] += 31  # past recovery window
        resp = run(router.call("sys", "hi"))
        assert resp.provider == "a", "half-open probe passes, 'a' handles request"
        assert router.breakers["a"].state.name == "CLOSED"
        assert router.breakers["a"].success_count >= 1
    finally:
        mod.time.time = real_time


def test_probe_failure_reopens_breaker():
    import brain.llm_router as mod
    router = build_router([
        make_provider("a", ["429", "429", "timeout"]),  # probe will time out
        make_provider("b", ["ok", "ok", "ok"]),
    ])
    real_time = time.time
    now = [real_time()]
    mod.time.time = lambda: now[0]
    try:
        for _ in range(2):
            run(router.call("sys", "hi"))
        assert router.breakers["a"].state.name == "OPEN"
        now[0] += 31
        resp = run(router.call("sys", "hi"))
        assert resp.provider == "b"
        assert router.breakers["a"].state.name == "OPEN", "probe failure re-opens breaker"
        assert router.breakers["a"].failure_count == 3
    finally:
        mod.time.time = real_time


def test_get_stats():
    router = build_router([
        make_provider("a", ["429"]),
        make_provider("b", ["ok"]),
    ])
    run(router.call("sys", "hi"))
    stats = router.get_stats()
    assert stats["last_used"] == "b"
    pa = stats["providers"]["a"]
    assert pa["failure_count"] == 1
    assert pa["last_error"] and "429" in pa["last_error"]
    pb = stats["providers"]["b"]
    assert pb["success_count"] == 1
    assert "state" in pa and "timeout" in pa and "model" in pa
    assert "ema_latency_ms" in pb


def test_adaptive_timeout_shrinks_and_recovers():
    router = build_router([
        make_provider("a", ["timeout", "ok"], base_timeout=10.0),
        make_provider("b", ["ok"]),
    ])
    run(router.call("sys", "hi"))
    assert router.providers[0].timeout == 7.5, "timeout shrinks 10 -> 7.5"
    run(router.call("sys", "hi"))
    assert router.providers[0].timeout == 8.125, "timeout recovers toward base"


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
    print(f"\n{passed}/{len(tests)} router tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
