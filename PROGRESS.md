# Alfred Progress Log

Read this first at the start of any work session (autonomous or not) —
it's the "what's actually done, what's in flight, what's blocked" record
so nobody (human or Claude) has to re-derive context from scratch. Append,
don't rewrite history — newest entries at the top.

---

## 2026-08-31 — Autonomy system wired up live

- Researched loop-engineering principles (5 sources) before building, per
  Sam's request — refined the design: time-boxed sessions, git isolation
  per run, hard verification not self-report, and (the research's own
  cautionary finding) starting with tighter check-ins than originally
  planned, loosening later.
- **Real constraint discovered while building, not assumed away**: the
  `schedule` skill's cloud routines run in Anthropic's cloud with zero
  access to Sam's local machine — no real server, no local vault, no
  live network calls, no local credentials. This rules out live
  verification from the cloud side entirely. Decision (Sam's, given this):
  cloud routine does **code-only** work — implementation + the mocked
  test suite only — and explicitly flags in every PR what still needs a
  local, live check before it's actually done. Not a hybrid, not
  pretending the cloud can do what it can't.
- Routine created: `trig_01U7DDqtuWKAsfWa6c2fU66E` ("Alfred autonomous
  code work"), every 2 hours (`7 */2 * * *` UTC), against
  `github.com/Vanaxity/Alfred`, working from `feature/day7-heartbeat`
  (not `main` — that branch has this week's real work, main is stale).
  First task: Q2's security audit.
- **Found and fixed before the first fire**: the routine auto-attached a
  Gmail MCP connector and a "Claude_Code_Remote" connector neither asked
  for nor wanted — a code-only agent should have zero live email access.
  Cleared via `clear_mcp_connections`, confirmed empty before letting it
  run. Caught by checking the actual creation response, not by assuming
  the request I sent was the request that got configured.
- Fail-safe rules baked into the routine's own prompt: branch off
  `feature/day7-heartbeat` only, never touch `main` or that branch
  directly, never self-merge, run the mocked suite before any "done"
  claim, one well-scoped unit of work per firing, stop and write up the
  question in the PR if genuinely stuck rather than guessing.

**Still open, by design**: every PR this produces still needs a real,
live-verified pass (local session, real server) before it's actually
done — the cloud side can implement and mock-test, not confirm the real
thing works.

## 2026-08-27 — Roadmap + autonomy system drafted

- `ROADMAP.md` written: Phase 1-4 by Sep 30, Phase 5 dropped (Sam's call).
- Autonomy-system design drafted, **not yet wired up live** — needs a
  separate explicit go-ahead before the recurring schedule actually runs.
- Week 1 priorities identified: order Phase 4 hardware now, Q2 security
  audit, Q8 speed audit.
- File-structure cleanup (roadmap item #1) done this session: `project-alfred`
  and `alfred-cockpit/server/` archived (not deleted), README pointer added,
  Graphify installed and spot-verified accurate.

**Status: nothing from Week 1 started yet.** This entry exists to bootstrap
the log, not to claim progress that hasn't happened.
