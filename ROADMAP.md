# Alfred Roadmap — Phase 1 → Phase 4 by September 30, 2026

**Written:** 2026-08-27, co-authored with Sam. First draft — meant to be
argued with, not accepted as-is.

**Scope decision (Sam's call, 2026-08-27):** Phase 5 (open-ended
self-modification) is dropped from this roadmap entirely — the manifesto
itself describes it as "Ongoing" with no ceiling, and pretending it fits in
five weeks would be exactly the kind of aspirational-not-actual claim this
whole project has been trying to stop making. Phase 1–4, in full, by
September 30.

**One hard constraint no amount of autonomous coding fixes:** Phase 4
includes physical hardware — a LilyGo T-Watch S3, home-automation devices.
Shipping takes real days. **Action for this week, not week 5: order the
hardware now**, so it arrives in parallel with the software work instead of
blocking week 5 entirely.

---

## Where things actually stand today (2026-08-27), not aspirationally

From this week's own live-verified work (see `ALFRED_MANIFESTO_V5.md`'s
dated status entries for the full detail):

- **Memory (T2/T3/T4/T5):** T3 strong, T4 reliable (Claim A: curation pass +
  `forget` + narrowed `memory_save`), T5 verified this week (found and fixed
  a real crash). T2 skill matching real but doesn't skip planning (that
  claim was retracted as false).
- **All 21 registered tools confirmed live-working** as of today (3 real
  bugs found and fixed getting there).
- **Auto-start on boot:** working, Task Scheduler-verified, local server +
  ngrok tunnel + Vercel cockpit deploy all confirmed end-to-end.
- **Still open from the original 9 launch questions** (Q1/Q3/Q6 closed):
  - **Q2 — security/data-leak audit: UNVERIFIED, and genuinely urgent now.**
    A real concern was flagged and never chased down: no confirmed auth
    check on `/chat` / `/api/command` in `brain_api/server.py`. Now that
    the ngrok tunnel is live and auto-starting on boot, this stops being a
    hypothetical — anyone with the tunnel URL may currently be able to read
    memory or send email as Sam. **This moves to week 1, not later.**
  - Q4 (per-person customization) — hardcoded to one user, "Master Sam."
    Not a goal for this roadmap unless Sam says otherwise — Alfred is a
    personal, single-user system by design; revisit only if that changes.
  - Q5 (non-technical install simplicity), Q7 (positioning), Q9 (value
    synthesis) — business/positioning questions, not blocking engineering
    work, revisit after Phase 2-3 land.
  - Q8 (UX feel) — now has real first-hand signal, not just a hypothesis:
    Sam's own first session reported slow replies and one recovered task
    failure. **Also week 1** — a speed audit before piling more phases on
    top of a system that already feels slow once.

---

## Week-by-week plan

### Week 1 (Aug 27 – Sep 2): Close Phase 1's real gaps, order hardware

- **Order Phase 4 hardware today** (LilyGo T-Watch S3, whatever home
  automation devices Phase 4 needs) — pure lead-time insurance, zero
  engineering cost to start now.
- **Q2 security audit.** Confirm or fix the missing-auth suspicion on
  `/chat` and `/api/command`. If confirmed, this is the single highest-
  priority fix in the whole roadmap — a live, publicly-tunneled assistant
  with no auth is a real exposure, not a nice-to-have.
- **Q8 speed audit.** Instrument where a turn's time actually goes
  (planning call / tool execution / the new memory-curation pass / T3
  search) and find what's parallelizable vs. genuinely sequential. Doesn't
  need to be fully solved this week, but needs a real answer, not a guess.
- Set up the autonomy system itself (see below) so weeks 2-5 can actually
  run semi-unattended.

### Week 2 (Sep 3 – 9): Phase 2 — MCP client + first real connectors

- Build `brain/mcp_client.py`: reads `mcp_servers.json`, spawns servers,
  discovers tools via `tools/list`, registers them dynamically — no
  hardcoding, per the manifesto's own spec.
- Ship 2-3 real connectors, prioritized by what Sam actually uses day to
  day over "impressive but unused": Filesystem MCP (near-free, already
  half-built via the existing safe-path file tools), then Sam's pick of
  Google Drive/Sheets/Docs or one messaging platform (Slack/Discord/
  Telegram/WhatsApp).
- Proactive memory surfacing (the relocated heartbeat + GBrain's
  confidence-gated push-context) — the manifesto already specs this as one
  build, not two.

### Week 3 (Sep 10 – 16): Phase 3 — Tool Forge, self-audit, entity graph

- Finish Tool Forge: the markdown-skill → executable-Python conversion path
  (skill used 3+ times → LLM-generated function → sandboxed validation →
  registered tool). `improve_skill()`'s wiring from this week is the down
  payment; this is the rest of it.
- Self-audit loop: weekly cron feeding Alfred its own execution logs,
  proposing one concrete optimization.
- Entity graph & synthesis (GBrain-inspired) — the actual "grows with you"
  mechanism Claim A explicitly deferred. Build this now that Claim A has
  held up under a real week of usage, per the manifesto's own stated
  precondition.

### Week 4 (Sep 17 – 23): Phase 4 software layer

- Full voice autonomy: wake word (openWakeWord), continuous conversation
  mode, SOUL.md-driven personality switching.
- Visual perception via screenshot analysis (the software half of "sees" —
  webcam/MediaPipe gesture control is physical-hardware-adjacent and can
  slip into week 5 if needed without blocking the rest).

### Week 5 (Sep 24 – 30): Phase 4 hardware + integration buffer

- Home Assistant MCP connection (lights, climate, locks).
- Twilio (SMS/calls) and LilyGo watch integration — hardware ordered week 1
  should have arrived by now.
- Buffer for whatever slipped, plus one real end-to-end pass across
  everything shipped this month (same live-verification discipline as
  Q3/Q6 this week — test it for real, don't just claim it).

---

## The autonomy system: working on Alfred while Sam's away

**The actual ask:** Sam doesn't want to sit and supervise every step. Wants
real progress to happen unattended for stretches of hours, with check-ins
when a decision genuinely needs a human — not a constant stream of pings,
and not silent unreviewed changes either.

### The fail-safe part (non-negotiable, already how this session works)

- **Every code change goes on a feature branch, gets tested, then becomes a
  PR. Nothing is ever pushed to `main` or merged without Sam reviewing it.**
  This is the actual safety mechanism — not "ask before every line," but
  "nothing reaches the real system without a human looking at it first."
  Same workflow already used for every PR this week.
- **The existing hard boundaries stay in force**: no entering credentials/
  tokens on Sam's behalf, no sending messages/emails, no real deployments,
  no destructive git operations — unchanged from how this whole week's
  work has already run. Autonomy doesn't mean loosening these; it means
  running the same rules for longer stretches without a human in the loop
  for the routine parts.
- **Verify before claiming done** — the discipline behind catching the T5
  crash, the screenshot-routing bug, and the web_fetch truncation this
  week — doesn't relax just because no one's watching in real time. If
  anything it matters more unattended, since there's no one to catch a
  false "done" in the moment.

### When to check in vs. just proceed

Check in (push notification, since Remote Control reaches your phone) for:
- Anything in the existing explicit-permission/prohibited categories above.
- A real fork in approach with no clearly-better default (e.g. "which
  messaging platform's connector first" if the roadmap didn't already
  decide it).
- A change big/hard-to-undo enough that showing the plan first is cheaper
  than unwinding it later (a real architectural refactor, not a routine
  fix).
- Genuinely stuck after real debugging effort — not "first sign of
  friction," matching how flakes vs. real bugs got told apart this week.
- A natural roadmap-item boundary, batched rather than mid-task — finishing
  Q2's audit is worth a ping; every intermediate grep isn't.

Just proceed for: routine implementation within an already-agreed roadmap
item, fixing something the way this week's bugs got fixed (find it, fix
it, verify it, commit to a branch), anything squarely inside the boundaries
above.

### How it actually runs (mechanism)

- A recurring scheduled agent (not the session-bound, 7-day-capped cron
  primitive — a durable scheduled routine) resumes work at a regular
  cadence, reads this roadmap plus a running status log to know exactly
  where things stand, and works the next item.
- The status log (`PROGRESS.md`, to be created alongside this file) is
  what makes each wake-up not have to re-derive context — same principle
  as Graphify for code structure, but for "what's done, what's in flight,
  what's blocked."
- Graphify's already-installed knowledge graph gets used (and kept current
  via `graphify update .`) instead of the agent re-reading the whole
  codebase from scratch each session.

**Not yet built — needs Sam's go-ahead before it goes live**, same as
Task Scheduler did this week: the actual recurring-schedule wiring. This
document is the design; making it real is a separate, explicit step.

---

## Explicitly not in this roadmap

- Phase 5 (dropped per Sam's decision above).
- Q4 (multi-tenancy) — not a goal unless Sam says otherwise.
- Anything not already in Phase 1-4 of the manifesto — no scope creep
  invented mid-roadmap without it being written here first.
