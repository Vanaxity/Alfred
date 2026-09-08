"""
Cognitive Heartbeat — ROADMAP.md Phase 3's "relocated heartbeat + GBrain's
confidence-gated push-context".

Context: a reminder/cron/heartbeat system existed once (`brain/v2/heartbeat.py`,
CognitiveHeartbeat wiring in Alfred.__init__, an alert broadcaster in
server.py) and was deliberately removed 2026-08-23 as a scope-reduction
ahead of a 2-day showcase deadline (see that commit's message) -- along with
the reminder-creation tools and local_db's reminder/cron query methods.
ROADMAP.md (written days later) planned to bring the heartbeat back for real
in Phase 3, this time closing the manifesto's actual complaint: "The
heartbeat is a simple cron, not a cognitive loop. It never asks, 'Based on
Master Sam's goals, is there anything missing right now?'"

This is that rebuild, not a revival of the old file. Scope, deliberately:
  - A light tick (every LIGHT_INTERVAL_S) checks due reminders and cron
    scheduled tasks via the local_db methods restored alongside this file,
    and clears expired T1 context.
  - A heavy tick (every HEAVY_INTERVAL_S) runs the actual cognitive pulse:
    gathers T4 goals, recent T3 episodes, and a best-effort calendar/email
    snapshot, then asks the LLM a single confidence-gated question per the
    manifesto's own spec.
  - Confidence gating is deliberately more conservative than the manifesto's
    literal text ("if high confidence, execute the corrective action"):
    this build only ever logs (low) or surfaces a nudge (medium/high) for a
    human to act on -- it never calls a mutating tool on its own. That
    matches ROADMAP.md's own fail-safe boundaries ("no sending messages/
    emails... no destructive actions" for anything running unattended) more
    than it matches the manifesto's most literal reading, and autonomous
    email/calendar writes from a background loop with no per-action human
    review is a materially different risk than a chat turn a human is
    actively watching. Revisit only if Sam explicitly asks for the
    auto-execute path.
  - No reminder-creation tools (set_reminder etc.) are rebuilt here --
    get_due_reminders() has nothing to return until those exist again.
    Tracked as a follow-up in PROGRESS.md, not silently assumed done.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional


class CognitiveHeartbeat:
    """Background proactive-cognition loop owned by one Alfred instance.

    `alfred` only needs `.memory` (FiveTierMemory), `.db` (LocalDB), and
    `._router` (LLMRouter) -- kept as attribute access rather than importing
    those types directly so tests can hand it a bare fake with just those
    three attributes (see build-system/test_cognitive_heartbeat.py).
    """

    LIGHT_INTERVAL_S = 30
    HEAVY_INTERVAL_S = 7200  # 2h, matches the old cadence

    SYSTEM_PROMPT = (
        "You are Alfred's proactive cognition. Given Master Sam's goals, "
        "recent activity, and any calendar/email snapshot below, identify "
        "any gap between his current state and his goals. Reply with a "
        "first line of exactly 'CONFIDENCE: high', 'CONFIDENCE: medium', "
        "or 'CONFIDENCE: low', then a blank line, then one short paragraph: "
        "high/medium confidence should read as a nudge worth interrupting "
        "him for; low confidence should just be a passing observation. If "
        "there is truly nothing worth surfacing, reply 'CONFIDENCE: low' "
        "with a one-line reason."
    )

    def __init__(self, alfred: Any, on_alert: Optional[Callable[[Dict], Awaitable[None]]] = None):
        self.alfred = alfred
        self.on_alert = on_alert
        self.enabled = False
        self._task: Optional[asyncio.Task] = None
        self._pending_alerts: List[Dict] = []
        self._last_heavy_run = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background loop. No-op if already running or if no
        event loop is currently running (matches the old code's guard --
        this must be called from within an async context, e.g. an app's
        startup lifespan, not from a plain sync __init__)."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            print("[Heartbeat] start() called with no running event loop -- skipped")
            return
        self.enabled = True
        self._task = loop.create_task(self._loop())
        print(f"[Heartbeat] started (light={self.LIGHT_INTERVAL_S}s, heavy={self.HEAVY_INTERVAL_S}s)")

    def stop(self) -> None:
        self.enabled = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        self._last_heavy_run = time.monotonic()
        while self.enabled:
            await asyncio.sleep(self.LIGHT_INTERVAL_S)
            try:
                await self.light_tick()
                if time.monotonic() - self._last_heavy_run >= self.HEAVY_INTERVAL_S:
                    self._last_heavy_run = time.monotonic()
                    await self.pulse()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Heartbeat] tick error: {e}")

    # ------------------------------------------------------------------
    # Light tick: reminders, cron, T1 expiration
    # ------------------------------------------------------------------

    async def light_tick(self) -> None:
        self._clear_expired_context()
        self._check_due_reminders()
        await self._check_scheduled_tasks()

    def _clear_expired_context(self) -> None:
        try:
            expired = self.alfred.memory.t1_clear_expired()
            if expired:
                print(f"[Heartbeat] cleared {expired} expired T1 items")
        except Exception as e:
            print(f"[Heartbeat] T1 expiration check failed: {e}")

    def _check_due_reminders(self) -> List[Dict]:
        try:
            due = self.alfred.db.get_due_reminders()
        except Exception as e:
            print(f"[Heartbeat] reminder check failed: {e}")
            return []
        fired = []
        for r in due:
            self.alfred.db.mark_reminder_fired(r["id"])
            alert = {"type": "reminder", "text": r["text"], "id": r["id"], "category": r.get("category", "general")}
            fired.append(alert)
            self._enqueue(alert)
        return fired

    async def _check_scheduled_tasks(self) -> None:
        try:
            due = self.alfred.db.get_due_scheduled_tasks()
        except Exception as e:
            print(f"[Heartbeat] scheduled-task check failed: {e}")
            return
        for task in due:
            try:
                result = await self.alfred.execute(task["task"])
                self.alfred.db.update_last_run(task["id"])
                output = (result or {}).get("response", "")
                if output:
                    self._enqueue({
                        "type": "cron",
                        "task": task["task"][:100],
                        "content": str(output)[:500],
                    })
            except Exception as e:
                print(f"[Heartbeat] scheduled task {task.get('id')} failed: {e}")

    # ------------------------------------------------------------------
    # Heavy tick: the actual cognitive pulse
    # ------------------------------------------------------------------

    async def pulse(self) -> Optional[Dict]:
        """Run one confidence-gated proactive-cognition pass. Returns the
        parsed {"confidence", "content"} result, or None if there was
        nothing worth reasoning about (skips the LLM call entirely)."""
        now = datetime.now()
        if now.hour < 7 or now.hour > 23:
            return None

        context = await self._gather_context()
        if not context:
            print("[Heartbeat] pulse: nothing to reason about, skipping LLM call")
            return None

        try:
            resp = await self.alfred._router.call(
                system_prompt=self.SYSTEM_PROMPT,
                user_message=context,
                max_tokens=250,
                temperature=0.4,
            )
            raw = (resp.text or "").strip()
        except Exception as e:
            print(f"[Heartbeat] pulse LLM call failed: {e}")
            return None

        if not raw:
            return None

        result = self._parse_pulse_response(raw)
        confidence = result["confidence"]
        content = result["content"]

        if confidence == "low":
            print(f"[Heartbeat] pulse (low confidence, logged only): {content[:200]}")
        else:
            alert = {"type": "cognitive_nudge", "confidence": confidence, "content": content[:500]}
            await self._enqueue_and_notify(alert)
            print(f"[Heartbeat] pulse ({confidence} confidence nudge): {content[:200]}")

        return result

    async def _gather_context(self) -> str:
        """Best-effort snapshot of goals/recent activity/calendar/email.
        Returns "" when there is genuinely nothing to reason about, so the
        caller can skip the LLM call rather than ask it to guess from
        nothing."""
        parts: List[str] = []

        goals = self._gather_goals()
        if goals:
            parts.append(f"Known goals/profile facts:\n{goals}")

        episodes = self._gather_recent_episodes()
        if episodes:
            parts.append(f"Recent activity:\n{episodes}")

        calendar = await self._gather_calendar()
        if calendar:
            parts.append(f"Calendar (next 7 days):\n{calendar}")

        email = await self._gather_email()
        if email:
            parts.append(f"Inbox triage:\n{email}")

        return "\n\n".join(parts)

    def _gather_goals(self) -> str:
        try:
            profile = self.alfred.memory.t4_load_profile()
        except Exception:
            return ""
        lines = []
        for section, data in (profile or {}).items():
            if not isinstance(data, dict):
                continue
            for key, value in list(data.items())[:5]:
                lines.append(f"- {section}.{key}: {str(value)[:100]}")
        return "\n".join(lines[:15])

    def _gather_recent_episodes(self) -> str:
        try:
            episodes = self.alfred.memory.t3_find_episodes("recent activity important tasks", max_results=5)
        except Exception:
            return ""
        return "\n".join(f"- {e.get('title', '(untitled)')}" for e in episodes)

    async def _gather_calendar(self) -> str:
        try:
            from ..tools.gws_client import GWSClient
            client = GWSClient()
            output = await asyncio.to_thread(client.get_agenda, days=7)
            if output and "No upcoming events" not in output:
                return output[:500]
        except Exception as e:
            print(f"[Heartbeat] calendar snapshot unavailable: {e}")
        return ""

    async def _gather_email(self) -> str:
        try:
            from ..tools.gws_client import GWSClient
            client = GWSClient()
            output = await asyncio.to_thread(client.triage_emails)
            if output and "No new emails" not in output:
                return output[:500]
        except Exception as e:
            print(f"[Heartbeat] email snapshot unavailable: {e}")
        return ""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    _CONFIDENCE_RE = re.compile(r"^\s*confidence:\s*(high|medium|low)\b", re.IGNORECASE)

    @classmethod
    def _parse_pulse_response(cls, raw: str) -> Dict[str, str]:
        """Parse the LLM's 'CONFIDENCE: <level>\\n\\n<content>' shape.
        Falls back to confidence='low' with the whole response as content
        if the model doesn't follow the format exactly -- an unparseable
        reply should degrade to "just log it", never get surfaced as if it
        were a confident nudge."""
        lines = raw.splitlines()
        match = cls._CONFIDENCE_RE.match(lines[0]) if lines else None
        if not match:
            return {"confidence": "low", "content": raw.strip()}
        confidence = match.group(1).lower()
        content = "\n".join(lines[1:]).strip()
        if not content:
            content = raw.strip()
        return {"confidence": confidence, "content": content}

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _enqueue(self, alert: Dict) -> None:
        self._pending_alerts.append(alert)

    async def _enqueue_and_notify(self, alert: Dict) -> None:
        self._enqueue(alert)
        if self.on_alert is not None:
            try:
                await self.on_alert(alert)
            except Exception as e:
                print(f"[Heartbeat] on_alert callback failed: {e}")

    def pop_alerts(self) -> List[Dict]:
        alerts = list(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts
