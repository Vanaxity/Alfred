"""
Cognitive Heartbeat -- ROADMAP.md Phase 3's "relocated heartbeat" item.

The v1 heartbeat (brain/alfred.py / brain/v2/alfred_v2.py) is dead code
under the current v2 rebuild -- brain/__init__.py's get_alfred() resolves
to brain.v2.conversation.Alfred, not that class, and its
_execute_heartbeat() calls methods (memory.t3_search(), db.get_due_reminders())
that no longer exist on the current FiveTierMemory/LocalDB. This module is
a fresh port into the v2 architecture, not a copy of that dead code.

It also upgrades the mechanism per the manifesto's own plan
(docs/MANIFESTO_V5.md, section 3 "The Cognitive Heartbeat"): instead of a
bare fetch-and-log cron, periodically hand the LLM Sam's stored profile/
goals (T4) plus recent episodes (T3) and ask it to name any concrete gap
and how confident it is that the gap is real -- gated by that confidence:

    high    -> a proposed corrective action, surfaced as an alert but never
               auto-executed (see _alert_from_pulse's docstring for why this
               deliberately departs from the manifesto's literal wording)
    medium  -> a nudge alert
    low     -> logged only (the common case -- most pulses should find
               nothing worth surfacing)

No email/calendar integration here -- that lives in brain/tools/gws_client.py
and is a separate concern. This module makes exactly one LLM call per tick
and is fully exercised in tests against fakes; it never runs a live network
call on its own.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

DEFAULT_INTERVAL_SECONDS = 3600

# Matches v1's window (brain/alfred.py's _execute_heartbeat: skip before 7am
# or after 11pm) -- a personal assistant pulsing Sam at 3am is a bug, not a
# feature.
ACTIVE_HOURS = range(7, 24)

_PULSE_SYSTEM_PROMPT = (
    "You are Alfred's proactive cognition, a background process running "
    "periodically -- not part of a live conversation, and Master Sam cannot "
    "see or respond to this turn directly. You are given his stored "
    "profile/goals and a few recent episode summaries. Your only job: "
    "decide whether there is a real, concrete gap between his stated goals "
    "and his recent activity -- not a vague musing.\n\n"
    "Respond with ONLY a JSON object, no other text:\n"
    '{"confidence": "high"|"medium"|"low", "observation": "one sentence", '
    '"action": "a concrete next step, or null"}\n\n'
    "confidence high: a critical, time-sensitive gap with an obvious fix "
    "(e.g. a deadline with visibly zero progress). confidence medium: a "
    "real gap worth a gentle nudge, not urgent. confidence low: nothing "
    "concrete enough to bother him with -- this should be the common case; "
    "do not manufacture a gap just to have something to report."
)


def _build_pulse_user_message(profile_context: str, episodes: List[Dict[str, str]]) -> str:
    parts = [profile_context.strip() or "(no stored profile/goals yet)"]
    if episodes:
        parts.append("\nRecent activity:")
        for ep in episodes:
            parts.append(f"- {ep['title']}: {ep['snippet']}")
    else:
        parts.append("\n(no recent episodes found)")
    return "\n".join(parts)


@dataclass
class PulseResult:
    confidence: str
    observation: str
    action: Optional[str] = None


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort extraction of the first top-level {...} object in `text`,
    tolerant of a model wrapping the JSON in prose or a code fence."""
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
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


async def run_cognitive_pulse(alfred: Any) -> Optional[PulseResult]:
    """One heartbeat tick's worth of proactive reasoning.

    Returns None if there's nothing to reason about yet (no profile, no
    episodes), the LLM call failed, or the reply couldn't be parsed into
    the expected shape -- this must fail silently and never crash the
    loop or surface an error to the user.
    """
    profile_context = ""
    try:
        profile_context = alfred.memory.get_context_for_llm() or ""
    except Exception:
        pass

    episodes: List[Dict[str, str]] = []
    try:
        found = alfred.memory.t3_find_episodes("recent activity and goals", max_results=5)
        for ep in found or []:
            title = ep.get("title", "untitled")
            episodes.append({"title": title, "snippet": ep.get("snippet", title)})
    except Exception:
        pass

    if not profile_context.strip() and not episodes:
        return None

    try:
        response = await alfred._router.call(
            _PULSE_SYSTEM_PROMPT,
            _build_pulse_user_message(profile_context, episodes),
            max_tokens=300,
            temperature=0.2,
        )
    except Exception:
        return None

    if not response or not getattr(response, "text", None):
        return None

    obj = _extract_json_object(response.text)
    if not obj:
        return None

    confidence = str(obj.get("confidence", "")).strip().lower()
    if confidence not in ("high", "medium", "low"):
        return None
    observation = str(obj.get("observation", "")).strip()
    if not observation:
        return None
    raw_action = obj.get("action")
    action = (
        str(raw_action).strip()
        if raw_action and str(raw_action).strip().lower() != "null"
        else None
    )

    return PulseResult(confidence=confidence, observation=observation, action=action)


def _alert_from_pulse(result: PulseResult) -> Optional[Dict[str, Any]]:
    """Confidence gating: low -> no alert (caller still counts the tick).
    medium/high -> a pending alert.

    A high-confidence result carries a `proposed_action`, but this never
    executes it autonomously. The manifesto's own wording for this feature
    says "if you find a critical gap with high confidence, execute the
    corrective action" -- but every other mutating capability shipped this
    week (MCP tools default require_approval=True, shell/run_code) is
    gated behind an explicit human approval specifically because letting
    the model act unsupervised is the exact failure mode Q2's security
    work was about. A background loop with no live conversation to attach
    an approval prompt to is the wrong place to be the first exception.
    Whether/how these proposals eventually reach a real approval surface
    is left as an open question for Sam -- not guessed at here.
    """
    if result.confidence == "low":
        return None
    alert_type = "heartbeat_action_proposal" if result.confidence == "high" else "heartbeat_nudge"
    alert: Dict[str, Any] = {
        "type": alert_type,
        "source": "cognitive_pulse",
        "confidence": result.confidence,
        "observation": result.observation,
        "timestamp": datetime.now().isoformat(),
    }
    if result.action:
        alert["proposed_action"] = result.action
    return alert


class Heartbeat:
    """Background cognitive-pulse loop for one Alfred instance.

    Mirrors the connect_mcp_servers() pattern: constructed eagerly but
    started explicitly (start()) from an async context (brain_api/server.py's
    startup lifespan), since spawning an asyncio.Task requires a running
    event loop that isn't available during Alfred's synchronous __init__.
    """

    def __init__(
        self,
        alfred: Any,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        on_alert: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        active_hours: range = ACTIVE_HOURS,
    ) -> None:
        self.alfred = alfred
        self.interval_seconds = interval_seconds
        self.on_alert = on_alert
        self.active_hours = active_hours
        self.enabled = True
        self.pulse_count = 0
        self.last_result: Optional[PulseResult] = None
        self._task: Optional["asyncio.Task[None]"] = None

    def start(self) -> None:
        """No-op if already started or no event loop is running yet (mirrors
        v1's _start_heartbeat try/except -- a failure here must never take
        down Alfred's own init/startup)."""
        if self._task is not None:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            self._task = loop.create_task(self._loop())

    def stop(self) -> None:
        self.enabled = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def tick(self) -> Optional[PulseResult]:
        """Run exactly one pulse right now, bypassing the interval sleep --
        what the loop calls each cycle, and what tests call directly."""
        if datetime.now().hour not in self.active_hours:
            return None
        result = await run_cognitive_pulse(self.alfred)
        self.pulse_count += 1
        self.last_result = result
        if result is None:
            return None
        alert = _alert_from_pulse(result)
        if alert is not None:
            alerts = getattr(self.alfred, "_pending_alerts", None)
            if alerts is not None:
                alerts.append(alert)
            if self.on_alert is not None:
                try:
                    await self.on_alert(alert)
                except Exception:
                    pass
        return result

    async def _loop(self) -> None:
        while self.enabled:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.tick()
            except Exception:
                pass
