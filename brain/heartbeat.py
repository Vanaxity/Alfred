"""
Heartbeat — background check for due reminders.

Relocated for brain/v2 (ROADMAP.md Phase 3's "relocated heartbeat"). The
pre-v2 architecture (brain/alfred.py, now dead code — see
brain/alfred_v2.py's module docstring) ran a `_heartbeat_loop` that polled
LocalDB for due reminders and cron tasks every 30s and pushed alerts over
WebSocket. The Hermes rebuild (brain/v2/) dropped this entirely, along with
the set_reminder/list_reminders/delete_reminder tools themselves — a
reminder could no longer even be set, let alone fire. This module is the
mechanical half of relocating that: poll LocalDB, fire what's due, hand it
to a caller-supplied broadcast function.

Deliberately reminders-only for now. scheduled_tasks (cron) still has no
creation tool anywhere in brain/v2/, so there is nothing yet to poll for —
left for a follow-up rather than adding a poller with nothing to check.

Also deliberately NOT the manifesto's "cognitive heartbeat" (confidence-
gated proactive reasoning over T4 goals/calendar/inbox — act on high
confidence, nudge on medium, log on low). That is a separate, larger piece
that depends on this mechanical loop existing first; not built here.

Kept dependency-free (asyncio + typing only, no brain_api/server.py or
brain/__init__.py imports) so it can be unit-tested with a fake db and a
fake broadcast function in this cloud sandbox, which cannot import the
full server (faiss/sentence-transformers aren't installed here).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict

Broadcast = Callable[[Dict[str, Any]], Awaitable[None]]

DEFAULT_INTERVAL_SECONDS = 30


async def check_due_reminders(db: Any, broadcast: Broadcast) -> int:
    """One heartbeat pass: fire every currently-due reminder.

    Marks each fired *before* broadcasting it, so a broadcast failure
    (e.g. no WebSocket clients connected) can't leave a reminder stuck
    re-firing forever on the next tick. Returns how many fired.
    """
    due = db.get_due_reminders()
    for reminder in due:
        db.mark_reminder_fired(reminder["id"])
        await broadcast({
            "type": "reminder",
            "id": reminder["id"],
            "text": reminder["text"],
            "category": reminder.get("category", "general"),
        })
    return len(due)


async def run_heartbeat_loop(
    db: Any,
    broadcast: Broadcast,
    interval: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Poll forever. Caller owns cancellation (e.g. via Task.cancel() on
    shutdown) — this never returns on its own."""
    while True:
        await asyncio.sleep(interval)
        try:
            await check_due_reminders(db, broadcast)
        except Exception as e:
            print(f"[Heartbeat] error: {e}")
