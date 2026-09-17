# Alfred Roadmap — reliability first, rescoped 2026-09-09

**Rescoped 2026-09-09, Sam's call.** Six months in, still not reliable
enough to actually rely on. The previous week-by-week plan (Phase 1→4 by
Sep 30) is replaced by a different ordering: **prove the core engine is
trustworthy before touching anything else.** UI, calls, and Phase 4 all
wait behind that, not because they don't matter, but because polishing
any of them on top of a loop that silently stalls mid-task is wasted
work — confirmed live this week: a request to "reverse-engineer yourself"
made Alfred announce "I'm going to read the files," then stop and wait,
with no error, no status, nothing.

**Explicit non-goal for this phase**: don't rebuild what already works.
Phase 1 (memory, tools, auth) and Phase 2 (generic MCP client) are done
and live-verified — see `PROGRESS.md`'s dated entries. This roadmap is
about the specific reliability gaps found this week, not a restart.

---

## Where things actually stand (2026-09-09)

- **Memory (T1-T5), the generic MCP client, and most of Phase 3's roadmap
  items are done** — heartbeat, self-audit, reminders, entity graph all
  reviewed, bugs fixed, sitting as clean PRs waiting for Sam to merge
  (`PROGRESS.md` has the full breakdown). Tool Forge is explicitly
  **not** done — a real, shared security gap (LLM-generated code runs
  once, unapproved, before any human sees it) was found across all three
  attempts at it; it needs a dedicated hardening pass, not a quick patch.
- **The core turn loop has two confirmed, unfixed reliability bugs**,
  live-reproduced this week (see Phase A below) — this is the actual
  reason "not reliable" and "stops after a long message" keep happening.
- **Calls run through the exact same heavy loop as everything else** —
  confirmed by reading the code, not assumed. No fast path, no turn
  budget of its own, so a call pays the cost of the worst-case chat turn.
- **The cloud routine is disabled** after producing a real PR pileup (17
  open PRs, heavy duplication) — see `PROGRESS.md`'s 2026-09-09 entry for
  what happened and why. Not resumed until Sam decides how (or whether).

---

## Phase A — Terminal-first reliability (now, ~2 days)

Goal: a long, real, multi-step task runs to actual completion in the
terminal — no UI, no calls, nothing hidden — or fails loudly and
specifically instead of silently.

1. **Fix the loop-termination bug.** `brain/v2/conversation.py`'s
   `execute()` treats *any* plain-text reply as a finished answer — there
   is no check for whether the reply actually answers the task versus
   just narrates what it's about to do next ("I'm going to read the
   files"). Confirmed live as the direct cause of tasks silently
   stalling. Fix: extend the existing untooled-completion-claim nudge
   (`_is_untooled_completion_claim`, already catches "has been saved"/"I
   need your approval" phrasings with no tool call behind them) to also
   catch stated-intent-with-no-tool-call, forcing another turn instead of
   accepting it as final.
2. **Fix the turn budget for open-ended work.** `MAX_TURNS = 10` is fine
   for a calculator question, hopeless for reading a real codebase. When
   a task genuinely won't finish in budget, the fallback must be a real
   status report — what's done, what's left, does it need more turns or
   a specific missing piece of information — not a generic apology.
3. **Live step-by-step visibility.** The full tool-call trace
   (`thinking`) currently comes back in one blob at the end of the whole
   request. Stream it out as each turn completes instead of buffering it
   — this is what "show me the reasoning like Claude Code does" actually
   requires.
4. **Real error surfacing.** A tool failure must reach the user as a
   specific, readable error, not get swallowed into a generic "I wasn't
   able to process that."
5. **Root-cause the self-hallucination bug** ("tell me about myself"
   inventing facts) — trace the real T4/T3 memory-context assembly live
   against Sam's actual profile/episode data, not guessed at.

## Phase B — Reliable execution at scale

Goal: a 20-step chain either finishes completely, or cleanly pauses
asking permission with a genuine yes/no status — never a silent stall.

1. Turn Phase A's status-report fallback into a real checkpoint: at a
   natural pause point, state exactly what can/can't be done and wait
   for a decision, rather than guessing past it or dying quietly.
2. Fix the approval-signature exact-match flake (the LLM doesn't always
   regenerate byte-identical params on retry, so a resend can fail to
   match the pending approval) — deferred for weeks, real contributor to
   "not reliable."
3. **Verify Phase B actually holds before Phase C starts** — DONE
   2026-09-16, live-tested against an isolated Obsidian vault, never
   Sam's real one. Found and fixed 5 real bugs in the process rather
   than just reporting them (see PR #49):
   - Guardrail enforcement: a destructive shell command was only
     approval-gated, not denied — the `Remove-Item...-Recurse` deny
     pattern excluded quote characters, so any command quoting its
     path (the normal case) slipped past it entirely. Fixed. The same
     deny-pattern list is shared by `run_code`, but every pattern was
     shell syntax — asking again made Alfred reach for Python's
     `shutil.rmtree(...)` instead, bypassing everything. Fixed.
     `open_app` correctly has **no** approval gate (matches #45,
     already merged) — the original wording above was stale, written
     before that merge. Path-traversal rejection and benign-shell
     approval-gating both already worked correctly.
   - Skill-learning loop: generation worked (a real `.md` appears on
     disk after a 3-tool, zero-failure task), but reuse didn't —
     generation was missing the inverse of its own improvement gate
     (`matched_skill is None`), so repeating an already-learned task
     wrote a redundant near-duplicate every time instead of reusing
     it. Fixed.
   - Cron execution: was completely dead, not just untested.
     `get_due_scheduled_tasks()` was called every 30s but never
     defined anywhere, and even fixing that wouldn't have mattered —
     the heartbeat loop that calls it was never started in
     `brain_api/server.py`'s actual entrypoint at all. Ported and
     wired in. A real, pre-existing "check my calendar" task (created
     2026-07-05) had been silently inert this whole time and will
     start actually running now.
   - Error recovery: already worked correctly — a nonexistent-file
     request was surfaced honestly, not confabulated.
   - Separately found, not fixed (out of scope for this pass): the
     `nuclear` MCP server (`mcp_servers.json`) hangs server startup
     indefinitely with no timeout when its backing service isn't
     running — real risk, needs its own look.

## Phase C — UI overhaul (only after A + B hold)

Bring Phase A's step-by-step visibility and honest status/error states
into the cockpit. Deliberately not started until the engine underneath
is trustworthy — no UI polish on top of a loop that can still silently
stall.

## Phase D — Voice & Calls

Split architecture: calls get a fast, short-response path (tight
turn/token budget, no 20-step chains live on a call); heavy or
long-running work is handed to a background job that reports back when
done instead of making the call wait. Separately: debug why audio isn't
playing at all right now — a concrete, live-testable bug, not designed
around yet.

## Phase E — Phase 4

Voice autonomy (openWakeWord, continuous conversation mode,
SOUL.md-driven personality switching), visual perception, then the
hardware layer (Home Assistant, Twilio, LilyGo watch) — unchanged in
substance from the original plan, picked up with whatever time remains
after Phases A-D actually hold. Hardware lead time is still real: order
anything Phase 4 needs as soon as this phase is reachable, not the week
it starts.

---

## Capability gaps found, not yet scoped (2026-09-14)

Surfaced by comparing Alfred's actual code against Sam's HERMES/OpenClaw
research notes — real differences, not committed to any phase yet, per
this roadmap's own no-scope-creep rule below:

- **No multi-channel gateway** — Alfred is reachable only via its own
  HTTP API + the cockpit; no Telegram/Discord/Slack/WhatsApp/Signal
  (confirmed zero code, not just an unimplemented doc mention).
- **No sub-agent/parallel delegation** — can't spawn isolated
  workstreams the way HERMES/Claude Code can; confirmed zero code
  outside the manifesto's aspirational text.
- **Cron sophistication unverified** — the scheduler itself is real and
  live (`local_db.py`'s `scheduled_tasks` table + the 30s heartbeat
  poll in `_check_scheduled_tasks()`), but whether natural language like
  "every morning at 8" reliably becomes a correct cron expression hasn't
  been tested — folded into the Phase B verification pass above.

---

## The autonomy system — on hold, lessons learned

The cloud routine is disabled (`PROGRESS.md` 2026-09-09 has the full
account). Two real, load-bearing findings from running it this week,
whichever way it gets resumed later:

1. **A fresh session has no memory of prior firings' work or their own
   stand-down requests** unless that's written into `PROGRESS.md` itself
   — a request left only in an unmerged PR body is invisible to the next
   firing, which will just redo the work. Whatever resumes this needs a
   real in-progress/claimed marker the next firing actually reads, not an
   honor-system PR comment.
2. **Disabling the scheduled trigger does not stop already-running
   sessions that subscribed to a PR's activity webhooks** from reacting
   to new comments/closes indefinitely. Found live: closing PRs today
   caused two more PRs from sessions that had subscribed to them, well
   after the trigger was off. A real kill-switch needs to account for
   this, not just the cron schedule.

Not resumed until Sam decides how to address both, or decides not to
resume it at all.

---

## Explicitly not in this roadmap right now

- Tool Forge (the LLM-writes-and-registers-code pipeline) — real
  security gap found, needs a dedicated hardening pass as its own
  scoped piece of work, not folded into Phase A-E.
- Q4 (multi-tenancy), Q5/Q7/Q9 (business/positioning) — unchanged,
  non-blocking, revisit after Phase A-D land.
- Anything not already listed above — no scope creep invented mid-roadmap
  without it being written here first.
