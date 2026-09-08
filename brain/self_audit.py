"""
Self-Audit Loop — ROADMAP.md Phase 3.

Reads Alfred's own recent execution_log rows (written by
brain/v2/conversation.py's Alfred.execute() after every turn) and asks the
LLM the manifesto's own self-review question: identify patterns of
inefficiency, errors, or user corrections, and propose ONE concrete
optimization.

Read-only and proposal-only by design: this never edits Alfred's own code or
config. "Alfred can suggest (or, in sandbox mode, implement)..." per the
manifesto -- the implement half is explicitly out of scope here, since a
cloud, unattended run has no way to live-verify a self-modification before
proposing it as done. The actual weekly cron trigger is a local step (same
category as Task Scheduler auto-start) -- this module only provides the
callable a cron, or a manual `self_audit` tool call, runs.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List


SELF_AUDIT_SYSTEM_PROMPT = (
    "You are Alfred, reviewing your own performance. You will be given a "
    "statistical summary of your own recent execution history. Review your "
    "performance for this period. Identify patterns of inefficiency, errors, "
    "or user corrections. Propose ONE concrete, specific optimization to "
    "your own code or configuration -- referencing the actual numbers/tools "
    "below, not a generic suggestion. Keep your answer under 200 words."
)


def summarize_executions(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce raw execution_log rows into the stats a self-audit prompt needs."""
    if not rows:
        return {"turn_count": 0}

    turn_count = len(rows)
    avg_total_ms = sum(r.get("total_ms", 0.0) or 0.0 for r in rows) / turn_count
    avg_turns_used = sum(r.get("turns_used", 0) or 0 for r in rows) / turn_count

    tool_counter: Counter = Counter()
    for r in rows:
        try:
            called = json.loads(r.get("tools_called") or "[]")
        except (json.JSONDecodeError, TypeError):
            called = []
        for t in called:
            tool_counter[t] += 1

    error_turns = sum(1 for r in rows if (r.get("tool_error_count") or 0) > 0)
    nudge_turns = sum(
        1 for r in rows
        if r.get("completion_claim_nudge") or r.get("time_mismatch_nudge")
    )
    max_turns_hit_count = sum(1 for r in rows if r.get("max_turns_hit"))

    return {
        "turn_count": turn_count,
        "avg_total_ms": round(avg_total_ms, 1),
        "avg_turns_used": round(avg_turns_used, 2),
        "top_tools": tool_counter.most_common(5),
        "error_turn_count": error_turns,
        "error_rate": round(error_turns / turn_count, 3),
        "nudge_turn_count": nudge_turns,
        "max_turns_hit_count": max_turns_hit_count,
    }


def format_summary_for_prompt(summary: Dict[str, Any]) -> str:
    if summary.get("turn_count", 0) == 0:
        return "No execution history recorded for this period."
    top_tools = ", ".join(f"{t}×{c}" for t, c in summary["top_tools"]) or "none"
    return "\n".join([
        f"- {summary['turn_count']} turns reviewed",
        f"- avg total turn time: {summary['avg_total_ms']:.0f}ms",
        f"- avg tool-loop turns per task: {summary['avg_turns_used']}",
        f"- turns with a tool error: {summary['error_turn_count']} ({summary['error_rate'] * 100:.1f}%)",
        f"- turns that needed an untooled-completion/time-mismatch nudge: {summary['nudge_turn_count']}",
        f"- turns that hit the max-turn limit: {summary['max_turns_hit_count']}",
        f"- most-used tools: {top_tools}",
    ])


async def run_self_audit(db, router, days: int = 7) -> Dict[str, Any]:
    """Read the last `days` of execution history and produce one proposal.

    `db` is a LocalDB (get_recent_executions/log_self_audit/get_recent_self_audits).
    `router` is an LLMRouter (async .call(system_prompt, user_message, ...)).
    Returns {"days", "summary", "proposal"}; also persists the proposal via
    db.log_self_audit so a future audit can see what was already raised.
    """
    rows = db.get_recent_executions(days=days)
    summary = summarize_executions(rows)
    summary_text = format_summary_for_prompt(summary)

    if summary.get("turn_count", 0) == 0:
        proposal = f"No execution history in the last {days} days — nothing to audit yet."
    else:
        prior = db.get_recent_self_audits(limit=1)
        prior_note = ""
        if prior:
            prior_note = (
                f"\n\nYour last self-audit proposed: {prior[0].get('proposal', '')[:400]}\n"
                "If the data below still shows the same pattern, say so plainly "
                "rather than repeating the identical proposal unchanged. If it's "
                "resolved, find the next-most-worthwhile pattern instead."
            )
        user_message = (
            f"Your execution record for the last {days} days:\n{summary_text}"
            f"{prior_note}\n\n"
            "Review your performance this period. Identify patterns of "
            "inefficiency, errors, or user corrections. Propose one concrete "
            "optimization to your own code or configuration."
        )
        resp = await router.call(
            system_prompt=SELF_AUDIT_SYSTEM_PROMPT,
            user_message=user_message,
            max_tokens=400,
            temperature=0.4,
        )
        proposal = (resp.text or "").strip() or "Self-audit LLM call returned no content."

    try:
        db.log_self_audit(days=days, summary_json=json.dumps(summary), proposal=proposal)
    except Exception:
        pass

    return {"days": days, "summary": summary, "proposal": proposal}
