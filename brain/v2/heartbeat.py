"""
Cognitive Heartbeat -- Phase 3's proactive-cognition loop.

Manifesto V5 section 3 spec'd this ("Upgrade _execute_heartbeat() from a
fetch command to a proactive reasoning prompt"); ROADMAP.md Week 3 lists it
as "proactive memory surfacing (relocated heartbeat + GBrain's confidence-
gated push-context)", carried over from Phase 2's rescoping.

Where this actually fits: brain/v2/conversation.py's Alfred -- the live
architecture brain/__init__.py and brain_api/server.py both actually import
-- never grew a heartbeat of its own. The only heartbeat in this repo lives
in brain/alfred.py and its byte-for-byte duplicate brain/v2/alfred_v2.py,
neither of which brain/__init__.py imports anymore (brain/alfred_v2.py is a
thin re-export shim pointing at brain.v2.conversation) and neither of which
brain_api/server.py ever calls. Confirmed dead by grep: zero live callers of
_start_heartbeat/_heartbeat_loop/_execute_heartbeat outside that unused
pair. So "relocate the heartbeat" understates the gap -- there is no
heartbeat in the code that actually runs today. This module builds one for
the current architecture; it does not port the old cron-only design, which
the manifesto itself already called out as the thing to replace ("The
heartbeat is a simple cron, not a cognitive loop").

Confidence gating (the manifesto's own spec, "3. The Cognitive Heartbeat"):
one LLM call over T1+T3+T4 context returns a structured verdict --

  - "high"   -- a concrete corrective action is proposed. NEVER auto-executed
                here: nobody is watching an unattended cycle to catch a wrong
                guess, so auto-running a mutating tool (send an email, delete
                a calendar event) off one heartbeat's confidence would be a
                materially bigger risk than the manifesto's one-line "execute
                the corrective action" accounts for. The proposal is built
                with the same _action_signature scheme tool_executor.py's
                approval gate already uses, ready to be queued through that
                same approve/deny flow -- wiring it into the live chat
                session so it actually reaches the cockpit's Approve/Deny
                buttons is the next step, not done in this pass (see
                PROGRESS.md for exactly what that needs).
  - "medium" -- logged as a nudge, surfaced via get_log()/the on_alert
                callback a caller can pass to Alfred.start_heartbeat() --
                never sent anywhere on its own.
  - "low" / no gap -- logged as a plain observation. Most cycles should land
                here; an empty T4 profile skips the LLM call entirely rather
                than asking a model to invent a goal from nothing.

No live LLM call has verified this cycle's prompt actually produces
well-formed JSON from a real model -- this cloud sandbox has no API keys and
no live server. Verified here only against fakes (build-system/test_heartbeat.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first balanced JSON object out of a possibly-chatty reply.

    A local copy of tool_executor.py's helper of the same shape, kept
    independent so this module has zero coupling to tool_executor's dispatch
    internals -- only to the plain-JSON-extraction idiom every LLM-facing
    part of this codebase already uses.
    """
    if not text:
        return None
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(obj, dict):
                    return obj
                start = -1
    return None


def _action_signature(tool_name: str, params: Dict[str, Any]) -> str:
    """Mirrors tool_executor._action_signature's exact canonicalization --
    a heartbeat-proposed action must produce the same signature a later
    approved_actions entry would need to match once this proposal is wired
    into the live chat approval flow."""
    canonical = json.dumps(params or {}, sort_keys=True, default=str)
    return f"{tool_name}:{canonical}"


VALID_CONFIDENCE = {"high", "medium", "low"}

SYSTEM_PROMPT = (
    "You are Alfred's proactive cognition -- a background process, not a "
    "reply to Master Sam. You never talk to him directly; you only decide "
    "whether anything needs his attention right now.\n\n"
    "Given his current goals/profile and recent context below, identify any "
    "gap between his stated goals and his current state (a deadline with no "
    "progress, a recurring commitment with nothing scheduled, a task he said "
    "he'd do that never happened). If you find nothing worth flagging, say "
    "so honestly -- most cycles should find nothing.\n\n"
    "Reply with ONLY one JSON object, no prose:\n"
    "{\"gap_found\": true|false, \"confidence\": \"high\"|\"medium\"|\"low\", "
    "\"observation\": \"<one sentence, empty string if gap_found is false>\", "
    "\"suggested_action\": {\"tool\": \"<tool name>\", \"params\": {...}} or null}\n\n"
    "confidence meanings: \"high\" = a specific, ready-to-run corrective "
    "action exists (suggested_action must then be non-null with a real tool "
    "name). \"medium\" = worth a nudge to Master Sam but not something to "
    "act on unasked. \"low\" = a minor observation, not worth surfacing. "
    "Never set confidence \"high\" without a concrete suggested_action."
)


@dataclass
class HeartbeatEntry:
    """One cycle's outcome."""
    type: str  # "idle" | "observation" | "nudge" | "proposal" | "error"
    confidence: str
    observation: str
    action: Optional[Dict[str, Any]] = None
    signature: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": self.type,
            "confidence": self.confidence,
            "observation": self.observation,
            "timestamp": self.timestamp,
        }
        if self.action is not None:
            d["action"] = self.action
        if self.signature is not None:
            d["signature"] = self.signature
        return d


class CognitiveHeartbeat:
    """Runs one proactive-cognition cycle at a time. Owns no scheduling of
    its own -- brain.v2.conversation.Alfred.start_heartbeat() drives the
    interval and the background task."""

    MAX_LOG = 200

    def __init__(self, memory: Any, router: Any) -> None:
        self.memory = memory
        self.router = router
        self._log: List[HeartbeatEntry] = []

    def get_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self._log[-limit:]]

    def _append(self, entry: HeartbeatEntry) -> HeartbeatEntry:
        self._log.append(entry)
        if len(self._log) > self.MAX_LOG:
            self._log = self._log[-self.MAX_LOG:]
        return entry

    async def run_cycle(self) -> HeartbeatEntry:
        """One proactive-cognition pass. Never raises -- any failure
        (context build, LLM call, JSON parse) becomes a logged "error"
        entry so a bad cycle can't crash the background loop driving it."""
        try:
            context = self.memory.get_context_for_llm()
        except Exception as e:
            return self._append(HeartbeatEntry(
                type="error", confidence="low",
                observation=f"Heartbeat could not read memory context: {e}",
            ))

        # Nothing to check a gap against yet -- skip the LLM call entirely
        # rather than asking the model to invent a "goal" from an empty
        # profile. A fresh install with no T4 profile/goals saved should
        # produce silent idle cycles, not a stream of hallucinated gaps.
        if not context or "## User Profile:" not in context:
            return self._append(HeartbeatEntry(
                type="idle", confidence="low",
                observation="No profile/goals recorded yet -- nothing to check.",
            ))

        try:
            resp = await self.router.call(
                system_prompt=SYSTEM_PROMPT,
                user_message=f"Current context:\n\n{context[:4000]}",
                messages=[],
                max_tokens=300,
                temperature=0.2,
            )
        except Exception as e:
            return self._append(HeartbeatEntry(
                type="error", confidence="low",
                observation=f"Heartbeat LLM call failed: {e}",
            ))

        parsed = _extract_json_object((getattr(resp, "text", None) or "").strip())
        if not isinstance(parsed, dict):
            return self._append(HeartbeatEntry(
                type="error", confidence="low",
                observation="Heartbeat LLM reply was not parseable JSON.",
            ))

        gap_found = bool(parsed.get("gap_found"))
        confidence = str(parsed.get("confidence", "low")).strip().lower()
        if confidence not in VALID_CONFIDENCE:
            confidence = "low"
        observation = str(parsed.get("observation") or "").strip()
        suggested = parsed.get("suggested_action")

        if not gap_found:
            return self._append(HeartbeatEntry(
                type="idle", confidence="low",
                observation=observation or "No gap found this cycle.",
            ))

        if confidence == "high" and isinstance(suggested, dict) and suggested.get("tool"):
            tool = str(suggested["tool"])
            params = suggested.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            return self._append(HeartbeatEntry(
                type="proposal", confidence="high",
                observation=observation or f"Proposing {tool} to close a gap.",
                action={"tool": tool, "params": params},
                signature=_action_signature(tool, params),
            ))
        if confidence == "high":
            # Claimed high confidence but gave nothing runnable -- treat as a
            # nudge rather than silently dropping the observation.
            confidence = "medium"

        if confidence == "medium":
            return self._append(HeartbeatEntry(
                type="nudge", confidence="medium",
                observation=observation or "Worth a nudge -- no detail given.",
            ))

        return self._append(HeartbeatEntry(
            type="observation", confidence="low",
            observation=observation or "Minor observation, nothing actionable.",
        ))
