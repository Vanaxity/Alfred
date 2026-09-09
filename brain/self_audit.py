"""
Self-audit loop (Phase 3, ROADMAP.md) -- Day 7.

Alfred keeps no structured record of its own turns today. `Alfred.execute()`
(brain/v2/conversation.py) builds a real per-phase `timings` dict and a
per-tool `tool_results` list every turn, but both die at the HTTP response
boundary -- `ChatResponse` (brain_api/server.py) never carries `timings`,
and `tool_results` isn't persisted either. The closest thing to a
persisted trace, T3 episodic memory (`brain/memory/five_tier.py`), is a
lossy human-readable summary: tool names only, no timings, no
success/failure. There was nothing for a "self-audit" to actually read.

This module adds the missing piece:
  - `log_turn_execution()` -- an append-only, fire-and-forget log of each
    turn's tool usage/failures/timings, called from `Alfred.execute()`
    right next to the existing T3-episode save (same non-fatal,
    try/except-guarded shape).
  - `read_execution_log()` / `aggregate_stats()` -- read it back and turn
    it into the small statistical summary an audit actually needs (no raw
    task/reply content leaves this module beyond an 80-char preview).
  - `propose_optimization()` / `run_self_audit()` -- ask the LLM for
    exactly one concrete, numbers-grounded optimization.

Deliberately NOT wired to a real schedule. The 2026-08-23 heartbeat/cron
removal (see PROGRESS.md) took the only generic "run this job
periodically" primitive out of the codebase, and re-adding one is out of
scope here -- ROADMAP.md's autonomy-system section already treats the
recurring-schedule question as a separate, explicit step. `run_self_audit()`
is a plain callable: invoke it by hand (`python -m brain.self_audit`), or
wire it into whatever local weekly trigger Sam sets up (e.g. the same
Windows Task Scheduler mechanism already verified for auto-start-on-boot).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

EXECUTION_LOG_PATH = Path(__file__).parent / "data" / "execution_log.jsonl"

# Duration keys from Alfred.execute()'s `timings` dict worth averaging.
# `turns_used` is a count, tracked separately in aggregate_stats().
_TIMING_KEYS = (
    "goal_expansion_ms", "skill_matching_ms", "memory_snippets_wait_ms",
    "pre_loop_total_ms", "prompt_build_ms", "llm_call_ms",
    "tool_execution_ms", "mutation_verify_ms", "compression_ms", "total_ms",
)

# total_ms/pre_loop_total_ms are sums of the other phases, not a phase in
# their own right -- excluded when picking the single "slowest phase".
_AGGREGATE_TIMING_KEYS = {"total_ms", "pre_loop_total_ms"}


def log_turn_execution(
    task: str,
    tools_called: List[str],
    tool_results: List[Dict[str, Any]],
    timings: Dict[str, Any],
    log_path: Optional[Path] = None,
) -> None:
    """Append one compact record of a completed turn to the execution log.

    Deliberately doesn't store the full task/reply text -- that already
    lives in T3 episodic memory when a real tool ran. Just enough to find
    optimization patterns: which tools ran, which failed and why, and
    where the turn's time went. Caller is expected to wrap this in its own
    try/except (see brain/v2/conversation.py) so a disk error here can
    never break a turn.
    """
    path = log_path or EXECUTION_LOG_PATH
    record = {
        "timestamp": datetime.now().isoformat(),
        "task_preview": task[:80],
        "tools_called": list(tools_called),
        "tool_failures": [
            {"tool": r.get("tool"), "detail": str(r.get("output") or "")[:150]}
            for r in tool_results
            if r.get("success") is False
        ],
        "timings": {k: timings[k] for k in _TIMING_KEYS if k in timings},
        "turns_used": timings.get("turns_used"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_execution_log(
    log_path: Optional[Path] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Read back every valid record, oldest first. Tolerant of a missing
    file (nothing logged yet) and of individual corrupt/partial lines (e.g.
    a crash mid-write) -- skips those instead of failing the whole read."""
    path = log_path or EXECUTION_LOG_PATH
    if not path.exists():
        return []

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None:
            try:
                ts = datetime.fromisoformat(record.get("timestamp", ""))
            except (TypeError, ValueError):
                continue
            if ts < since:
                continue
        records.append(record)
    return records


def aggregate_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn raw execution-log records into the summary a self-audit prompt
    needs: per-tool call/failure counts, average per-phase timings, and
    which phase dominates total turn time on average."""
    turn_count = len(records)
    if turn_count == 0:
        return {"turn_count": 0}

    tool_calls: Dict[str, int] = {}
    tool_failures: Dict[str, int] = {}
    timing_sums: Dict[str, float] = {}
    timing_counts: Dict[str, int] = {}
    turns_used_total = 0

    for r in records:
        for tool in r.get("tools_called") or []:
            tool_calls[tool] = tool_calls.get(tool, 0) + 1
        for failure in r.get("tool_failures") or []:
            tool = failure.get("tool") or "unknown"
            tool_failures[tool] = tool_failures.get(tool, 0) + 1
        for key, val in (r.get("timings") or {}).items():
            if not isinstance(val, (int, float)):
                continue
            timing_sums[key] = timing_sums.get(key, 0.0) + val
            timing_counts[key] = timing_counts.get(key, 0) + 1
        turns_used_total += r.get("turns_used") or 0

    avg_timings = {key: timing_sums[key] / timing_counts[key] for key in timing_sums}
    phase_candidates = {
        k: v for k, v in avg_timings.items() if k not in _AGGREGATE_TIMING_KEYS
    }
    slowest_phase = max(phase_candidates, key=phase_candidates.get) if phase_candidates else None

    return {
        "turn_count": turn_count,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "avg_timings_ms": avg_timings,
        "slowest_phase": slowest_phase,
        "avg_turns_per_task": turns_used_total / turn_count,
        "date_range": {
            "earliest": records[0].get("timestamp"),
            "latest": records[-1].get("timestamp"),
        },
    }


_AUDIT_SYSTEM_PROMPT = (
    "You are Alfred's own self-audit process, reviewing a statistical "
    "summary of your recent turns (tool usage, failure counts, and "
    "per-phase timing averages -- no user content). Propose exactly ONE "
    "concrete, actionable optimization grounded in the numbers given, not "
    "a generic best practice. If the numbers don't clearly point to one, "
    "say so plainly instead of inventing one. Reply in plain text, 2-4 "
    "sentences, no preamble."
)


async def propose_optimization(stats: Dict[str, Any], router: Any = None) -> Dict[str, Any]:
    """Ask the LLM for one concrete optimization grounded in `stats`.
    Makes no LLM call (and needs none) when there isn't enough data yet,
    or when no router was supplied."""
    if stats.get("turn_count", 0) == 0:
        return {
            "proposal": "Not enough execution history yet -- no turns logged in this audit window.",
            "based_on_turns": 0,
        }
    if router is None:
        return {
            "proposal": None,
            "based_on_turns": stats["turn_count"],
            "error": "no LLM router configured",
        }

    user_message = f"Execution stats for the audit window:\n{json.dumps(stats, indent=2, default=str)}"
    resp = await router.call(
        system_prompt=_AUDIT_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=300,
        temperature=0.2,
    )
    return {
        "proposal": (resp.text or "").strip(),
        "based_on_turns": stats["turn_count"],
    }


async def run_self_audit(
    lookback_days: int = 7,
    log_path: Optional[Path] = None,
    router: Any = None,
) -> Dict[str, Any]:
    """Entry point: read the last `lookback_days` of execution-log records,
    aggregate them, and propose one concrete optimization.

    `router` must expose the same `async def call(system_prompt=,
    user_message=, ...)` interface as `brain.llm_router.LLMRouter` (see
    `_build_real_router()` below for how a real one gets built; tests
    inject a fake). Not wired to any scheduler -- see this module's
    docstring.
    """
    since = datetime.now() - timedelta(days=lookback_days)
    records = read_execution_log(log_path=log_path, since=since)
    stats = aggregate_stats(records)
    result = await propose_optimization(stats, router)
    return {"stats": stats, **result}


def _build_real_router() -> Any:
    """Same construction Alfred.__init__ uses for self._router -- imported
    lazily so importing this module never pulls in the groq/openai/
    google-genai SDKs unless this path actually runs."""
    import os
    from .llm_router import LLMRouter

    return LLMRouter(
        groq_key=os.environ.get("GROQ_API_KEY", ""),
        gemini_key=os.environ.get("GOOGLE_API_KEY", ""),
        openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )


def main() -> int:
    import asyncio

    result = asyncio.run(run_self_audit(router=_build_real_router()))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
