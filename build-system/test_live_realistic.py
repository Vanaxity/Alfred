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

# Windows' console codepage can't encode every character a live LLM response
# might contain (e.g. fullwidth punctuation) -- replacing the unprintable ones
# beats crashing mid-suite and losing every result gathered so far.
sys.stdout.reconfigure(errors="replace")

# preflight() below reads GROQ_API_KEY straight from os.environ, and it runs
# before anything imports brain.v2.conversation (the module that would
# otherwise load .env as a side effect). Without this, preflight() checks an
# empty key, sends an empty Bearer token, and Groq's 401 gets misread as "key
# rejected" when the key was simply never loaded yet.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

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


def _check_provider(name: str, host: str, url: str, api_key: str) -> str:
    """Return '' if reachable, else a human-readable reason. Shared by all
    three provider checks in preflight() -- kept provider-agnostic on
    purpose: LLMRouter itself fails over across providers, whichever is
    configured as priority-1 changes over time (already has this session),
    and a preflight hardcoded to one provider gives a false "everything is
    down" when only that one specific provider is having a bad day."""
    import socket
    import urllib.error
    import urllib.request

    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror as e:
        return f"{name}: DNS resolution failed ({e})"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "alfred-live-suite/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200:
                return ""
            return f"{name}: HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return f"{name}: rate limited (429)"
        if e.code in (401, 403):
            return f"{name}: API key rejected ({e.code})"
        return f"{name}: HTTP {e.code}"
    except Exception as e:
        return f"{name}: {e}"


def preflight() -> str:
    """Return '' if ANY configured provider is reachable, else a combined
    human-readable reason. Provider-agnostic by design -- see
    _check_provider()."""
    checks = []
    if os.environ.get("OPENROUTER_API_KEY"):
        checks.append(("openrouter", "openrouter.ai",
                        "https://openrouter.ai/api/v1/models",
                        os.environ.get("OPENROUTER_API_KEY", "")))
    if os.environ.get("GROQ_API_KEY"):
        checks.append(("groq", "api.groq.com",
                        "https://api.groq.com/openai/v1/models",
                        os.environ.get("GROQ_API_KEY", "")))
    if os.environ.get("GOOGLE_API_KEY"):
        checks.append(("gemini", "generativelanguage.googleapis.com",
                        "https://generativelanguage.googleapis.com/v1beta/models?key="
                        + os.environ.get("GOOGLE_API_KEY", ""), ""))

    if not checks:
        return "No provider API keys set (GROQ_API_KEY / OPENROUTER_API_KEY / GOOGLE_API_KEY)."

    reasons = []
    for name, host, url, key in checks:
        reason = _check_provider(name, host, url, key)
        if not reason:
            return ""  # at least one provider reachable -- good to go
        reasons.append(reason)

    return "All configured providers unreachable: " + "; ".join(reasons)


def looks_starved(result):
    resp = (result.get("response") or "").strip().lower()
    return bool(resp) and not (result.get("tools_called") or []) and any(
        m in resp for m in _NO_RESPONSE_MARKERS
    )


async def run_one(alfred, scenario):
    """Single-turn: one question in, one graded answer out."""
    try:
        result = await alfred.execute(scenario["question"], {})
    except Exception as e:
        return False, [f"EXCEPTION: {e}"], {}, False
    if looks_starved(result):
        return False, ["NO LLM RESPONSE (provider/network problem, not behavior)"], result, True
    passed, reasons = grade(scenario, result)
    return passed, reasons, result, False


async def run_turns(alfred, scenario):
    """Multi-turn: replay scenario['turns'] in one growing conversation.

    History is threaded exactly the way the real API does it (server.py's
    process_chat reads/writes context["conversation_history"] the same
    shape) -- a local [{"role","content"}, ...] list grown after each turn,
    not a DB-backed session, so this never touches real session state.

    Overall pass requires every turn to pass. A starved turn (no LLM
    response) aborts the rest of the scenario rather than continuing on a
    conversation that's missing a turn — a later turn "passing" against
    broken history would be a false signal, not a real result.
    """
    history = []
    turn_results = []
    overall_reasons = []
    for i, turn in enumerate(scenario["turns"]):
        try:
            result = await alfred.execute(turn["question"], {"conversation_history": list(history)})
        except Exception as e:
            turn_results.append({"turn": i + 1, "passed": False, "starved": False,
                                  "reasons": [f"EXCEPTION: {e}"], "response": ""})
            overall_reasons.append(f"turn {i + 1}: EXCEPTION: {e}")
            return False, overall_reasons, turn_results, False

        if looks_starved(result):
            turn_results.append({"turn": i + 1, "passed": False, "starved": True,
                                  "reasons": ["NO LLM RESPONSE"], "response": ""})
            return False, [f"turn {i + 1}: no LLM response, aborting rest of conversation"], turn_results, True

        passed, reasons = grade(turn, result)
        response_text = result.get("response") or ""
        turn_results.append({
            "turn": i + 1, "passed": passed, "starved": False, "reasons": reasons,
            "response": response_text[:400], "tools_called": result.get("tools_called") or [],
        })
        if not passed:
            overall_reasons.append(f"turn {i + 1}: " + "; ".join(reasons))

        history.append({"role": "user", "content": turn["question"]})
        history.append({"role": "assistant", "content": response_text})

    overall_passed = all(t["passed"] for t in turn_results)
    return overall_passed, overall_reasons, turn_results, False


async def run_scenario(alfred, scenario):
    """Dispatch by shape. Returns (passed, reasons, detail, starved) where
    detail is a single result dict for single-turn or a list of per-turn
    dicts for multi-turn."""
    if "turns" in scenario and scenario["turns"]:
        return await run_turns(alfred, scenario)
    return await run_one(alfred, scenario)


async def main_async(args):
    if not args.skip_preflight:
        reason = preflight()
        if reason:
            print(f"\n  PREFLIGHT FAILED: {reason}")
            print("  Aborting before running scenarios — a full run right now would")
            print("  produce 18 meaningless failures. Use --skip-preflight to force.\n")
            return 2

    # Isolate this run's T2/T3/T4 memory writes from the real Obsidian vault.
    # Confirmed live: every prior test run wrote learned-skill and episodic-
    # memory files straight into the user's real vault -- 813 T3 episodes and
    # 11 T2 skills accumulated there from months of test runs, indistinguishable
    # from genuine usage, and directly contributed to a context-bleed bug (a
    # just-saved test episode outscoring on recency and leaking into an
    # unrelated later question). OBSIDIAN_VAULT_PATH is read once at import
    # time by brain.memory.five_tier, so this MUST be set before that module
    # (or anything importing it, like brain.v2.conversation) is ever imported.
    if not args.pollute_real_vault:
        test_vault = Path(__file__).parent / ".test_vault"
        for sub in ("T1-Context", "T2-Skills", "T3-Episodic", "T4-UserProfile", "T5-Archive"):
            (test_vault / "Memory" / sub).mkdir(parents=True, exist_ok=True)
        os.environ["OBSIDIAN_VAULT_PATH"] = str(test_vault)

        # Seed T4 (Sam.md) so profile-dependent scenarios (t4_favorite_food_recall
        # and friends) have something real to recall -- a fresh isolated vault
        # otherwise starts with an empty profile and those scenarios fail for a
        # reason that has nothing to do with the code being tested. Reuses
        # seed_t4.py's own logic via direct file import (build-system/ has a
        # hyphen, so it isn't a valid dotted package for a normal import).
        sam_md = test_vault / "Memory" / "T4-UserProfile" / "Sam.md"
        if not sam_md.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "seed_t4", Path(__file__).parent / "seed_t4.py"
            )
            seed_t4 = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(seed_t4)
            seed_t4.main()

        print(f"  Using isolated test vault: {test_vault}\n")

    # Import after sys.path setup and the vault-path override above.
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
            is_multi = "turns" in sc and sc["turns"]
            passed, reasons, detail, starved = await run_scenario(alfred, sc)

            if is_multi:
                rows.append({"id": sc["id"], "category": sc["category"], "passed": passed,
                             "starved": starved, "reasons": reasons, "multi_turn": True,
                             "turns": detail})
                mark = "[PASS]" if passed else ("[SKIP]" if starved else "[FAIL]")
                print(f"  {mark} {sc['id']} ({len(detail)} turn(s))")
                if not passed:
                    for t in detail:
                        if not t["passed"]:
                            print(f"           turn {t['turn']}: {'; '.join(t['reasons']) or '?'}")
                            snippet = " ".join((t.get('response') or '').split())[:130]
                            print(f"                     got: {snippet!r}")
            else:
                result = detail
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
    ap.add_argument("--pollute-real-vault", action="store_true",
                    help="write T2/T3/T4 memory to the real OBSIDIAN_VAULT_PATH "
                         "instead of an isolated .test_vault -- off by default; "
                         "months of runs without this flag already polluted the "
                         "real vault with 800+ fake episodes, see build-system/README")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
