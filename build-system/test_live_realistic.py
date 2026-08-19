"""
Live realistic-question suite — the reality check the mocked suites can't be.

Run manually (makes REAL LLM calls, costs tokens, needs API keys):
    python build-system/test_live_realistic.py
    python build-system/test_live_realistic.py --category academics
    python build-system/test_live_realistic.py --id trig_tower_elevation_distractor
    python build-system/test_live_realistic.py --repeat 3     # catch flakiness

Why this exists
---------------
Every other suite in build-system/ scripts the LLM's response with a fake router.
That proves a given input is handled correctly, but it structurally CANNOT catch
what a live model actually chooses to do with a real question. Two real bugs got
past 85 passing mocked tests and were only found by asking Alfred an actual
homework question:

  * "angle of elevation 35 degrees, find the height" -> answered "53 feet"
    (correct: 33). No tool call. The safe-eval rejected the model's correct
    expression `47*tan(35*pi/180)` because it allowed no function calls, so with
    no working path to an answer the model invented one.
  * A math explanation came back as raw JSON, because the model wrote LaTeX
    (`\\(x^2\\)`) inside the JSON envelope and `\\(` isn't a valid JSON escape.

Scenarios live in live_scenarios.json next to this file so they can be edited
without touching harness code.

Grading
-------
A scenario passes when ALL of:
  (a) at least one of expect_substrings appears in the response (case-insensitive)
  (b) every tool in expect_tools appears in tools_called
  (c) no tool in forbid_tools appears in tools_called

(b) is the part that matters most: it fails a *right answer arrived at without a
tool*, which is a lucky guess, not working behavior. A model that says "It's
Tuesday" without calling `time` is guessing and will eventually be wrong.

These are probabilistic. A single failure is a signal to investigate, not proof
of a bug — use --repeat to tell a real regression from model variance.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SCENARIO_FILE = Path(__file__).parent / "live_scenarios.json"


def load_scenarios():
    return json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))


# Models emit typographic punctuation constantly: "don't" comes back as
# "don’t", "..." as "…", "-" as "–". Comparing those against
# ASCII-typed expectations silently false-fails correct answers -- observed
# live, where a perfect "I don't have that information" was marked FAIL purely
# because the apostrophe was curly. Normalize both sides before matching.
_PUNCT_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "…": "...",
}


def norm(text: str) -> str:
    """Lowercase and fold typographic punctuation to ASCII for comparison."""
    for a, b in _PUNCT_FOLD.items():
        text = text.replace(a, b)
    return text.lower()


def grade(scenario, result):
    """Return (passed, list_of_failure_reasons)."""
    response = (result.get("response") or "")
    lowered = norm(response)
    called = result.get("tools_called") or []
    reasons = []

    wanted = scenario.get("expect_substrings") or []
    if wanted and not any(norm(w) in lowered for w in wanted):
        reasons.append(f"none of {wanted[:4]} in response")

    for t in scenario.get("expect_tools") or []:
        if t not in called:
            reasons.append(f"expected tool {t!r} not called (called: {called})")

    # expect_any_tools: at least one must have been used. For cases where
    # several tools are equally correct (e.g. remember vs memory_save both
    # persist a fact) and pinning one would test an implementation guess.
    any_of = scenario.get("expect_any_tools") or []
    if any_of and not any(t in called for t in any_of):
        reasons.append(f"none of {any_of} called (called: {called})")

    for t in scenario.get("forbid_tools") or []:
        if t in called:
            reasons.append(f"forbidden tool {t!r} was called")

    return (not reasons), reasons


#  Alfred's own no-response fallbacks. If one of these comes back with no tools
#  called, the model gave us nothing usable — an infrastructure problem, not a
#  behavioral one. Misreading it as a regression sends you hunting a bug that
#  isn't there; both of these happened while building this suite:
#    * a 12/18 run dropped to 5/18 purely from provider rate limiting, and
#      every "new failure" passed again when run individually
#    * a later run scored 0/18 because the network was down entirely
#      (getaddrinfo failed), which an earlier version of this file cheerfully
#      mislabeled "likely rate limited"
#  Hence preflight() below: fail fast and loudly rather than emit 18 confident
#  wrong diagnoses.
_NO_RESPONSE_MARKERS = (
    "i wasn't able to process that request",
    "all ai providers are currently unavailable",
    "i'm not sure how to handle that",
    "malformed response from the model",
)


def preflight() -> str:
    """Return '' if the LLM is reachable, else a human-readable reason."""
    import socket
    import urllib.error
    import urllib.request

    try:
        socket.getaddrinfo("api.groq.com", 443)
    except socket.gaierror as e:
        return f"DNS resolution failed ({e}) — no network. Nothing here can run."

    # A real User-Agent is required, not optional: Groq sits behind Cloudflare,
    # which rejects urllib's default UA with a 403 (error 1010). Without this
    # header the preflight reports "API key rejected" on a perfectly good key.
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={
            "Authorization": f"Bearer {os.environ.get('GROQ_API_KEY', '')}",
            "User-Agent": "alfred-live-suite/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200:
                return ""
            return f"Groq /models returned HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "Groq is rate limiting (HTTP 429). Wait for the window to reset."
        if e.code in (401, 403):
            return f"Groq rejected the API key (HTTP {e.code}). Check GROQ_API_KEY."
        return f"Groq returned HTTP {e.code}"
    except Exception as e:
        return f"Cannot reach Groq: {e}"


def looks_starved(result):
    resp = (result.get("response") or "").strip().lower()
    return bool(resp) and not (result.get("tools_called") or []) and any(
        m in resp for m in _NO_RESPONSE_MARKERS
    )


async def run_one(alfred, scenario):
    try:
        result = await alfred.execute(scenario["question"], {})
    except Exception as e:
        return False, [f"EXCEPTION: {e}"], {}, False
    if looks_starved(result):
        return False, ["NO LLM RESPONSE (provider/network problem, not behavior)"], result, True
    passed, reasons = grade(scenario, result)
    return passed, reasons, result, False


async def main_async(args):
    if not args.skip_preflight:
        reason = preflight()
        if reason:
            print(f"\n  PREFLIGHT FAILED: {reason}")
            print("  Aborting before running scenarios — a full run right now would")
            print("  produce 18 meaningless failures. Use --skip-preflight to force.\n")
            return 2

    # Import after sys.path setup. Neutralize the heartbeat's background thread:
    # it ticks immediately on construction and would race these calls.
    from brain.v2 import heartbeat as hb_module
    hb_module.CognitiveHeartbeat.start = lambda self: None
    from brain.v2.conversation import Alfred

    scenarios = load_scenarios()
    if args.category:
        scenarios = [s for s in scenarios if s["category"] == args.category]
    if args.id:
        scenarios = [s for s in scenarios if s["id"] == args.id]
    if not scenarios:
        print("No scenarios matched those filters.")
        return 1

    alfred = Alfred()
    rows = []

    for rep in range(args.repeat):
        if args.repeat > 1:
            print(f"\n{'=' * 72}\n  PASS {rep + 1} of {args.repeat}\n{'=' * 72}")
        current = None
        for sc in scenarios:
            if sc["category"] != current:
                current = sc["category"]
                print(f"\n  -- {current} " + "-" * (58 - len(current)))
            passed, reasons, result, starved = await run_one(alfred, sc)
            rows.append({"id": sc["id"], "category": sc["category"], "passed": passed,
                         "starved": starved, "reasons": reasons,
                         "response": (result.get("response") or "")[:400],
                         "tools_called": result.get("tools_called") or []})
            mark = "[PASS]" if passed else ("[SKIP]" if starved else "[FAIL]")
            print(f"  {mark} {sc['id']}")
            if not passed:
                for r in reasons:
                    print(f"           ! {r}")
                if not starved:
                    snippet = " ".join((result.get("response") or "").split())[:150]
                    print(f"           got: {snippet!r}")
            if args.delay:
                await asyncio.sleep(args.delay)

    total = len(rows)
    passed_n = sum(1 for r in rows if r["passed"])
    starved_n = sum(1 for r in rows if r.get("starved"))
    graded = total - starved_n
    print(f"\n{'=' * 72}")
    print(f"  {passed_n}/{graded} passed" + (f"  ({starved_n} skipped — no LLM response)" if starved_n else ""))

    by_cat = {}
    for r in rows:
        if r.get("starved"):
            continue
        c = by_cat.setdefault(r["category"], [0, 0])
        c[1] += 1
        if r["passed"]:
            c[0] += 1
    for cat, (p, t) in sorted(by_cat.items()):
        print(f"    {cat:16s} {p}/{t}")

    failures = [r for r in rows if not r["passed"] and not r.get("starved")]
    if failures:
        print("\n  Failed:")
        for r in failures:
            print(f"    - {r['id']}: {r['reasons'][0] if r['reasons'] else '?'}")

    if starved_n:
        print(f"\n  {starved_n} scenario(s) got no LLM response at all. That is an")
        print("  infrastructure problem (rate limit, network, provider outage), not")
        print("  Alfred behaving badly — do not read it as a regression. Re-run one")
        print("  with --id to confirm, or --delay 10 to pace the suite harder.")

    report = Path(__file__).parent / "live_results.json"
    report.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Full transcript: {report}")
    print("=" * 72)

    # Probabilistic by nature -- report, don't gate a build on a single run.
    return 0


def main():
    ap = argparse.ArgumentParser(description="Live realistic-question suite for Alfred")
    ap.add_argument("--category", help="academics | personal_ops | memory_recall | adversarial")
    ap.add_argument("--id", help="run a single scenario by id")
    ap.add_argument("--repeat", type=int, default=1, help="run N times to gauge flakiness")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds to wait between scenarios; pacing avoids provider "
                         "rate limits that look like failures (default 3, use 0 to disable)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="run even if the LLM looks unreachable")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
