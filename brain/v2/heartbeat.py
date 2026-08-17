"""
Cognitive Heartbeat — Hermes-inspired proactive reasoning and cron.

Upgrades the bare reminder-check thread into a cognitive loop that:
    (a) Checks due reminders (existing path).
    (b) Queries the SQLite cron store for due scheduled tasks.
    (c) Runs a proactive-reasoning LLM call that receives:
        - Master Sam's top-level T4 goals
        - A summary of upcoming calendar events
        - Recent inbox snippets
        - A short recap of recent conversation
    (d) Pushes any resulting alerts/nudges via the WebSocket mechanism.

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
        self._pending_alerts: List[Dict[str, Any]] = []

    @property
    def pending_alerts(self) -> List[Dict[str, Any]]:
        return list(self._pending_alerts)

    def pop_alerts(self) -> List[Dict[str, Any]]:
        """Pop pending alerts for WebSocket broadcast."""
        alerts = list(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts

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

        Returns a list of alerts/nudges generated during this tick.
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

        # --- (d) Queue alerts for WebSocket broadcast ---
        self._pending_alerts.extend(alerts)

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
                self._db.mark_reminder_fired(r["id"])
                alert = {
                    "type": "reminder",
                    "text": r["text"],
                    "id": r["id"],
                    "category": r.get("category", "general"),
                    "timestamp": datetime.now().isoformat(),
                }
                alerts.append(alert)
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

        Sends Alfred a prompt asking it to identify gaps between Master Sam's
        goals and current state, then generates nudges for critical items.
        """
        alerts: List[Dict[str, Any]] = []

        # Gather context
        goals = self._get_goals_summary()
        calendar = self._get_calendar_summary()
        inbox = self._get_inbox_summary()

        if not goals and not calendar and not inbox:
            return alerts  # Nothing to reason about

        # Build proactive reasoning prompt
        prompt = self._build_proactive_prompt(goals, calendar, inbox)

        try:
            resp = await self._router.call(
                system_prompt="You are Alfred's proactive cognition module. "
                "Analyze Master Sam's situation and identify critical gaps. "
                "Be direct and actionable. No empty platitudes.",
                user_message=prompt,
                max_tokens=400,
                temperature=0.3,
            )
            response = (resp.text or "").strip()

            # If the LLM identified something actionable, create an alert
            if response and len(response) > 20:
                # Check if it's a genuine insight (not just "no issues")
                lower = response.lower()
                no_issues = [
                    "no issues", "no gaps", "everything looks good",
                    "no action needed", "nothing to report",
                ]
                if not any(phrase in lower for phrase in no_issues):
                    alert = {
                        "type": "proactive_nudge",
                        "message": response[:500],
                        "timestamp": datetime.now().isoformat(),
                    }
                    alerts.append(alert)

        except Exception as e:
            print(f"[Heartbeat] Proactive reasoning LLM call failed: {e}")

        return alerts

    def _get_goals_summary(self) -> str:
        """Get a summary of Master Sam's goals from T4 profile."""
        try:
            ctx = self._memory.get_context_for_llm()
            if ctx and "goals" in ctx.lower():
                # Extract just the goals section
                lines = ctx.split("\n")
                goal_lines = []
                in_goals = False
                for line in lines:
                    if "goal" in line.lower():
                        in_goals = True
                    if in_goals:
                        goal_lines.append(line)
                        if len(goal_lines) > 10:
                            break
                return "\n".join(goal_lines) if goal_lines else ctx[:500]
            return ctx[:500] if ctx else ""
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

    def _build_proactive_prompt(
        self, goals: str, calendar: str, inbox: str
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

        parts.extend([
            "## Task",
            "Based on the above, identify:",
            "1. Any deadline or event approaching with no preparation.",
            "2. Any goal that lacks a concrete next step.",
            "3. Any email that requires a response.",
            "",
            "If you find a critical gap, describe it in 1-2 sentences and suggest a concrete action.",
            "If everything looks good, say: 'No critical gaps detected.'",
        ])

        return "\n".join(parts)
