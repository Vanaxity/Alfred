"""
Cognitive Heartbeat — Hermes-inspired proactive reasoning and cron.

Upgrades the bare reminder-check thread into a cognitive loop that:
    (a) Checks due reminders (existing path).
    (b) Queries the SQLite cron store for due scheduled tasks and executes them.
    (c) Runs a proactive-reasoning LLM call that receives:
        - Master Sam's top-level T4 goals
        - A summary of upcoming calendar events
        - Recent inbox snippets
        - A short recap of recent T3 activity
      and returns a confidence-tiered verdict: high confidence attempts the
      action directly (through the same guardrail-checked path as any other
      tool call — a proactive action never self-approves), medium confidence
      becomes a nudge alert, low confidence is logged only.
    (d) Pushes alerts into Alfred.push_alert(), the single queue
        brain_api/server.py's broadcaster actually drains.

Cron jobs are stored in the existing `scheduled_tasks` table in local_db.py
(using croniter for expression parsing). The heartbeat runs on a configurable
interval (default 1800s = 30 minutes).
"""

from __future__ import annotations

import asyncio
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .tool_executor import _extract_json_object


class CognitiveHeartbeat:
    """
    Cognitive heartbeat with proactive reasoning and cron support.

    Usage:
        heartbeat = CognitiveHeartbeat(alfred, db, router, memory)
        await heartbeat.tick()  # one full cycle
        heartbeat.start()       # background loop
        heartbeat.stop()        # graceful shutdown
    """

    DEFAULT_INTERVAL = 1800  # 30 minutes in seconds

    def __init__(
        self,
        alfred: Any,
        db: Any,
        router: Any,
        memory: Any,
        interval: int = DEFAULT_INTERVAL,
    ) -> None:
        self._alfred = alfred
        self._db = db
        self._router = router
        self._memory = memory
        self._interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat background loop."""
        if self._running:
            return
        self._running = True

        def _run_background() -> None:
            """Run heartbeat ticks in a background thread."""
            while self._running:
                try:
                    # Create a new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self.tick())
                    loop.close()
                except Exception as e:
                    print(f"[Heartbeat] Tick error: {e}")
                time.sleep(self._interval)

        t = threading.Thread(target=_run_background, daemon=True)
        t.start()
        print(f"[Heartbeat] Started (interval={self._interval}s)")

    def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        print("[Heartbeat] Stopped")

    # ------------------------------------------------------------------
    # Tick — one full heartbeat cycle
    # ------------------------------------------------------------------

    async def tick(self) -> List[Dict[str, Any]]:
        """
        Execute one heartbeat cycle.

        Returns a list of alerts/nudges generated during this tick (also
        pushed into Alfred.push_alert() as they're created).
        """
        alerts: List[Dict[str, Any]] = []

        # --- (a) Check due reminders ---
        reminder_alerts = self._check_reminders()
        alerts.extend(reminder_alerts)

        # --- (b) Check due cron tasks ---
        cron_alerts = await self._check_cron_tasks()
        alerts.extend(cron_alerts)

        # --- (c) Proactive reasoning ---
        try:
            proactive_alerts = await self._proactive_reasoning()
            alerts.extend(proactive_alerts)
        except Exception as e:
            print(f"[Heartbeat] Proactive reasoning failed: {e}")

        # --- (d) Push alerts to the one queue the broadcaster drains ---
        for a in alerts:
            self._alfred.push_alert(a)

        if alerts:
            print(f"[Heartbeat] Generated {len(alerts)} alert(s)")

        return alerts

    # ------------------------------------------------------------------
    # (a) Reminder check
    # ------------------------------------------------------------------

    def _check_reminders(self) -> List[Dict[str, Any]]:
        """Check for due reminders (existing path)."""
        alerts: List[Dict[str, Any]] = []
        try:
            due = self._db.get_due_reminders()
            for r in due:
                alert = {
                    "type": "reminder",
                    "text": r["text"],
                    "id": r["id"],
                    "category": r.get("category", "general"),
                    "timestamp": datetime.now().isoformat(),
                }
                alerts.append(alert)
                # Mark fired only after the alert is queued: firing first
                # (the old order) meant a reminder due during any gap between
                # "mark fired" and "alert actually delivered" was lost for
                # good, with no way to recover it short of a DB inspection.
                self._db.mark_reminder_fired(r["id"])
        except Exception as e:
            print(f"[Heartbeat] Reminder check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # (b) Cron task check
    # ------------------------------------------------------------------

    async def _check_cron_tasks(self) -> List[Dict[str, Any]]:
        """Query the SQLite cron store for due scheduled tasks and execute them."""
        alerts: List[Dict[str, Any]] = []
        try:
            due_tasks = self._db.get_due_scheduled_tasks()
            for task in due_tasks:
                task_id = task.get("id")
                task_text = task.get("task", "")
                if not task_text:
                    continue

                print(f"[Heartbeat] Executing cron task: {task_text[:60]}")

                # Execute the task through the conversation loop
                try:
                    result = await self._alfred.execute(task_text)
                    response = result.get("response", "")

                    # Mark as executed
                    self._db.mark_scheduled_task_run(task_id)

                    alert = {
                        "type": "cron_task",
                        "task": task_text,
                        "response": response[:500],
                        "task_id": task_id,
                        "timestamp": datetime.now().isoformat(),
                    }
                    alerts.append(alert)
                except Exception as e:
                    print(f"[Heartbeat] Cron task execution failed: {e}")

        except Exception as e:
            print(f"[Heartbeat] Cron check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # (c) Proactive reasoning
    # ------------------------------------------------------------------

    async def _proactive_reasoning(self) -> List[Dict[str, Any]]:
        """
        Run a proactive-reasoning LLM call.

        Asks for a structured, confidence-tiered verdict rather than free
        text, so this code can branch instead of treating every non-trivial
        response identically:
            high   -> attempt the action (guardrail-checked, never self-approved)
            medium -> nudge alert (the old behavior)
            low    -> logged only, no alert
            malformed output -> treated as medium, never silently dropped
        """
        alerts: List[Dict[str, Any]] = []

        # Gather context
        goals = self._get_goals_summary()
        calendar = self._get_calendar_summary()
        inbox = self._get_inbox_summary()
        recap = self._get_recent_activity_recap()

        if not goals and not calendar and not inbox and not recap:
            return alerts  # Nothing to reason about

        prompt = self._build_proactive_prompt(goals, calendar, inbox, recap)

        try:
            resp = await self._router.call(
                system_prompt=(
                    "You are Alfred's proactive cognition module. Analyze Master "
                    "Sam's situation and identify critical gaps. Be direct and "
                    "actionable. No empty platitudes.\n"
                    "Output ONE JSON object, nothing else:\n"
                    '{"confidence": "high"|"medium"|"low", "observation": "...", '
                    '"action": {"tool": "name", "params": {...}} or null}\n'
                    "confidence=high ONLY when there is a concrete, safe, "
                    "immediately-executable action with tool+params you are "
                    "confident about. confidence=medium for a real gap worth "
                    "surfacing but with no safe automatic action (or none you're "
                    "confident enough to run). confidence=low for a minor or "
                    "speculative observation not worth interrupting for. "
                    'If nothing notable, set observation to "No critical gaps '
                    'detected." with confidence "low" and action null.'
                ),
                user_message=prompt,
                max_tokens=400,
                temperature=0.3,
            )
            raw = (resp.text or "").strip()
            parsed = _extract_json_object(raw)

            if not isinstance(parsed, dict) or "confidence" not in parsed:
                # Malformed/unparseable output must not vanish silently --
                # that's the exact failure mode this rewrite exists to fix.
                # Default to a medium-style nudge using the raw text, same as
                # the module's pre-tiering behavior.
                if raw and len(raw) > 20 and "no critical gap" not in raw.lower():
                    alerts.append({
                        "type": "proactive_nudge",
                        "message": raw[:500],
                        "timestamp": datetime.now().isoformat(),
                    })
                return alerts

            confidence = str(parsed.get("confidence", "medium")).strip().lower()
            observation = str(parsed.get("observation", "")).strip()
            action = parsed.get("action")

            if not observation or "no critical gap" in observation.lower():
                return alerts

            if confidence == "high" and isinstance(action, dict) and action.get("tool"):
                alert = await self._attempt_proactive_action(observation, action)
                if alert:
                    alerts.append(alert)
            elif confidence == "low":
                print(f"[Heartbeat] Low-confidence observation (logged only): {observation[:120]}")
            else:
                # medium, or "high" without a usable action -- still worth a nudge.
                alerts.append({
                    "type": "proactive_nudge",
                    "message": observation[:500],
                    "timestamp": datetime.now().isoformat(),
                })

        except Exception as e:
            print(f"[Heartbeat] Proactive reasoning LLM call failed: {e}")

        return alerts

    async def _attempt_proactive_action(
        self, observation: str, action: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Execute a high-confidence proactive action.

        Goes through the same ToolExecutor guardrail path as any user-issued
        tool call, with an empty approved_actions -- a proactive action must
        clear the same approval gate a human-issued one would, never skip it.
        """
        tool_name = action.get("tool")
        params = action.get("params") or {}
        tool_executor = getattr(self._alfred, "_tool_executor", None)
        if tool_executor is None:
            return None

        tool_ctx = {
            "memory": self._memory,
            "db": self._db,
            "router": self._router,
            "bootstrap": getattr(self._alfred, "_bootstrap", {}),
            "approved_actions": [],
        }

        try:
            result = await tool_executor.execute(tool_name, params, tool_ctx)
        except Exception as e:
            print(f"[Heartbeat] Proactive action execution error: {e}")
            return None

        if result.metadata.get("awaiting_approval"):
            # No synchronous requester to hand this back to (unlike a chat
            # turn) -- push it as an alert instead, same payload shape
            # Day 6's reactive approval path uses, not a second invented one.
            return {
                "type": "approval_request",
                "tool": result.metadata.get("tool", tool_name),
                "params": result.metadata.get("params", params),
                "signature": result.metadata.get("signature"),
                "observation": observation,
                "timestamp": datetime.now().isoformat(),
            }

        if result.success:
            return {
                "type": "proactive_action_taken",
                "tool": tool_name,
                "observation": observation,
                "result": str(result.output)[:300],
                "timestamp": datetime.now().isoformat(),
            }

        # Failed for a reason other than needing approval -- still surface
        # the observation rather than silently dropping it.
        return {
            "type": "proactive_nudge",
            "message": f"{observation} (attempted {tool_name} but it failed: {result.error})",
            "timestamp": datetime.now().isoformat(),
        }

    def _get_goals_summary(self) -> str:
        """Read T4's structured 'goals' section directly.

        The old version called get_context_for_llm() (T1 + full T4 dump) then
        substring-scanned every line for the word "goal" -- crude, and the
        live profile already has a clean `## goals` section with flat
        key:value entries to read directly instead.
        """
        try:
            profile = getattr(self._memory, "_t4_profile", None)
            if not profile:
                # Lazily loaded on first t4_get/t4_set in this process; if the
                # heartbeat's first tick fires before any of those, force it.
                self._memory.t4_load_profile()
                profile = getattr(self._memory, "_t4_profile", None)
            goals = (profile or {}).get("goals", {})
            if not goals:
                return ""
            lines = [f"{k}: {v}" for k, v in list(goals.items())[:10]]
            return "\n".join(lines)
        except Exception:
            return ""

    def _get_calendar_summary(self) -> str:
        """Get a summary of upcoming calendar events."""
        try:
            from ..tools.gws_client import GWSClient
            client = GWSClient()
            agenda = client.get_agenda(days=3)
            if agenda and "error" not in agenda.lower():
                return agenda[:500]
            return ""
        except Exception:
            return ""

    def _get_inbox_summary(self) -> str:
        """Get a summary of recent inbox emails."""
        try:
            from ..tools.gws_client import GWSClient
            client = GWSClient()
            emails = client.triage_emails()
            if emails and "error" not in emails.lower():
                return emails[:500]
            return ""
        except Exception:
            return ""

    def _get_recent_activity_recap(self) -> str:
        """A short recap of the most recent T3 episodes, by recency.

        The module docstring has claimed a "recent conversation recap" was
        part of the proactive prompt since this file was written; it never
        was -- _build_proactive_prompt() only ever took goals/calendar/inbox.
        """
        try:
            from ..memory.five_tier import T3_EPISODIC_DIR
            files = sorted(
                T3_EPISODIC_DIR.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:3]
            snippets = []
            for f in files:
                try:
                    snippets.append(f.read_text(encoding="utf-8")[:200])
                except Exception:
                    continue
            return "\n---\n".join(snippets)
        except Exception:
            return ""

    def _build_proactive_prompt(
        self, goals: str, calendar: str, inbox: str, recap: str = ""
    ) -> str:
        """Build the proactive reasoning prompt."""
        parts = [
            "You are Alfred's proactive cognition. Analyze the following and identify any critical gaps.",
            "",
        ]

        if goals:
            parts.append(f"## Master Sam's Goals\n{goals}\n")

        if calendar:
            parts.append(f"## Upcoming Calendar (next 3 days)\n{calendar}\n")

        if inbox:
            parts.append(f"## Recent Inbox\n{inbox}\n")

        if recap:
            parts.append(f"## Recent Activity\n{recap}\n")

        parts.extend([
            "## Task",
            "Based on the above, identify:",
            "1. Any deadline or event approaching with no preparation.",
            "2. Any goal that lacks a concrete next step.",
            "3. Any email that requires a response.",
            "",
            "Respond with the JSON object described in your system prompt.",
        ])

        return "\n".join(parts)
