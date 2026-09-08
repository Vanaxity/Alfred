"""
Cognitive Heartbeat — background proactive loop.

Day 7 of the original build plan (docs/PHASES.md), never actually built in
brain/v2: the only heartbeat that ever existed was the dead brain/alfred.py
path, which called LocalDB methods (`get_due_reminders`,
`get_due_scheduled_tasks`) that never existed anywhere in this codebase and
was never wired into the live v2 Alfred class at all.

Three things happen each tick():
    1. Check due reminders and fire them.
    2. Run any scheduled (cron) tasks that are due, through the real
       Alfred.execute() loop — same tool access, same guardrails, as a
       live turn.
    3. One lightweight proactive-reasoning LLM call against the user's T4
       profile — "is there a gap here worth surfacing," not a full agent
       turn with tool access. Per the original Day 7 acceptance criteria,
       it's fine for this to come back empty most ticks; it's the seed for
       ROADMAP.md's Phase 3 "confidence-gated push-context" work, not that
       system itself — no entity graph, no calibrated confidence score yet,
       just "does the model think there's something worth a nudge."

Alerts land in an in-memory queue (`pop_alerts()`), the same shape the old
alfred.py used (`{"type", "source", "content", ...}`) so a caller — a
websocket broadcast loop in brain_api/server.py, most likely — can drain it
without a new alert schema to learn.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional


class CognitiveHeartbeat:
    """Background proactive loop for one Alfred instance.

    Runs in its own daemon thread with its own asyncio event loop — Alfred
    itself may or may not have a running loop at the point start() is
    called (e.g. brain_api/server.py's lifespan handler), and a dedicated
    thread means a slow or hung tick (a stuck LLM call, a runaway cron
    task) can never block the request-handling loop.
    """

    DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes, per docs/PHASES.md Day 7

    def __init__(self, alfred: Any, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
        self._alfred = alfred
        self.interval_seconds = interval_seconds

        self._pending_alerts: List[Dict[str, Any]] = []
        self._alerts_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background thread. No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="alfred-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stop_event.is_set():
                try:
                    loop.run_until_complete(self.tick())
                except Exception as e:
                    # A failed tick must never kill the daemon thread -- the
                    # next tick 30 minutes from now should still happen.
                    self._push_alert({
                        "type": "heartbeat_error",
                        "source": "tick",
                        "content": f"{type(e).__name__}: {e}"[:500],
                    })
                # Interruptible sleep: stop() doesn't have to wait out a
                # full 30-minute interval to take effect.
                self._stop_event.wait(self.interval_seconds)
        finally:
            loop.close()

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def pop_alerts(self) -> List[Dict[str, Any]]:
        """Return and clear every alert queued since the last pop."""
        with self._alerts_lock:
            alerts = list(self._pending_alerts)
            self._pending_alerts.clear()
        return alerts

    def _push_alert(self, alert: Dict[str, Any]) -> None:
        alert.setdefault("timestamp", datetime.now().isoformat())
        with self._alerts_lock:
            self._pending_alerts.append(alert)

    # ------------------------------------------------------------------
    # One full cycle
    # ------------------------------------------------------------------

    async def tick(self) -> List[Dict[str, Any]]:
        """Run one full heartbeat cycle. Returns the alerts generated this
        tick (they're also pushed into pop_alerts(), same as any other
        alert) -- returning them too makes the cycle directly testable
        without going through the pop/clear queue."""
        generated: List[Dict[str, Any]] = []

        generated.extend(self._check_reminders())
        generated.extend(await self._run_due_cron_tasks())

        reasoning_alert = await self._proactive_reasoning(generated)
        if reasoning_alert is not None:
            generated.append(reasoning_alert)

        for alert in generated:
            self._push_alert(alert)
        return generated

    # ------------------------------------------------------------------
    # Step 1: reminders
    # ------------------------------------------------------------------

    def _check_reminders(self) -> List[Dict[str, Any]]:
        db = getattr(self._alfred, "db", None)
        if db is None:
            return []
        alerts = []
        try:
            due = db.get_due_reminders()
        except Exception as e:
            return [{"type": "heartbeat_error", "source": "reminders", "content": str(e)[:500]}]
        for reminder in due:
            alerts.append({
                "type": "heartbeat",
                "source": "reminder",
                "content": reminder.get("text", ""),
                "reminder_id": reminder.get("id"),
            })
            try:
                db.mark_reminder_fired(reminder["id"])
            except Exception:
                pass
        return alerts

    # ------------------------------------------------------------------
    # Step 2: due cron tasks
    # ------------------------------------------------------------------

    async def _run_due_cron_tasks(self) -> List[Dict[str, Any]]:
        db = getattr(self._alfred, "db", None)
        if db is None:
            return []
        try:
            due = db.get_due_scheduled_tasks()
        except Exception as e:
            return [{"type": "heartbeat_error", "source": "cron", "content": str(e)[:500]}]

        alerts = []
        for scheduled in due:
            task_text = scheduled.get("task", "")
            try:
                # Same execution path as a live turn -- full tool access,
                # guardrails, mutation verification. A cron task that needs
                # human approval (shell, run_code, ...) will surface that in
                # its result rather than actually running unattended, same
                # as it would for a live user.
                result = await self._alfred.execute(task_text, {})
                alerts.append({
                    "type": "heartbeat",
                    "source": "cron",
                    "content": str(result.get("response", ""))[:500],
                    "task": task_text,
                })
            except Exception as e:
                alerts.append({
                    "type": "heartbeat_error",
                    "source": "cron",
                    "content": f"'{task_text}' failed: {e}"[:500],
                })
            try:
                db.update_last_run(scheduled["id"])
            except Exception:
                pass
        return alerts

    # ------------------------------------------------------------------
    # Step 3: proactive reasoning
    # ------------------------------------------------------------------

    _REASONING_SYSTEM_PROMPT = (
        "You are Alfred's proactive cognition, running unprompted in the "
        "background -- Master Sam is not asking you anything right now. "
        "You are given Master Sam's stored profile (standing facts, "
        "preferences, recurring commitments) and a short summary of what "
        "this heartbeat cycle already did. Decide whether there is a "
        "genuine, concrete gap or follow-up worth surfacing right now -- "
        "not a generic check-in and not manufactured busywork. "
        "If nothing stands out, reply with exactly {\"reply\": \"nothing\"}. "
        "If something is genuinely worth a nudge, reply with "
        "{\"reply\": \"<one short, specific sentence>\"}. "
        "Output exactly one JSON object, nothing else."
    )

    async def _proactive_reasoning(
        self, cycle_alerts: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        router = getattr(self._alfred, "_router", None)
        memory = getattr(self._alfred, "memory", None)
        if router is None:
            return None

        profile = ""
        if memory is not None:
            try:
                profile = memory.get_context_for_llm()
            except Exception:
                profile = ""

        cycle_summary = (
            "\n".join(
                f"- {a['source']}: {a['content'][:200]}"
                for a in cycle_alerts
                if a.get("type") == "heartbeat"
            )
            or "nothing fired this cycle"
        )
        user_message = (
            f"Master Sam's profile:\n{profile[:2000] or '(empty)'}\n\n"
            f"What already happened this heartbeat cycle:\n{cycle_summary}\n\n"
            "Is there anything worth surfacing right now?"
        )

        try:
            resp = await router.call(
                system_prompt=self._REASONING_SYSTEM_PROMPT,
                user_message=user_message,
                max_tokens=200,
                temperature=0.2,
            )
        except Exception:
            return None

        raw = (resp.text or "").strip()
        if not raw:
            return None

        # Reuse Alfred's own lenient JSON-reply parser rather than
        # duplicating it -- same tolerance for the shapes a model actually
        # emits (nested tool/params, LaTeX backslashes, plain prose).
        parse = getattr(self._alfred, "_parse_llm_output", None)
        reply = raw
        if callable(parse):
            parsed_reply, _tool, _params = parse(raw)
            if parsed_reply is not None:
                reply = parsed_reply

        if not reply or reply.strip().lower() in ("nothing", "nothing.", ""):
            return None

        return {"type": "heartbeat", "source": "reasoning", "content": reply.strip()[:500]}
