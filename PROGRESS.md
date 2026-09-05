# Alfred Progress Log

Read this first at the start of any work session (autonomous or not) —
it's the "what's actually done, what's in flight, what's blocked" record
so nobody (human or Claude) has to re-derive context from scratch. Append,
don't rewrite history — newest entries at the top.

---

## 2026-09-05 — Q2 fixed for real; cloud routine root-caused (not a prompt problem)

- **Diagnosed why the autonomous cloud routine "kept failing"**: it never
  actually failed at the work. Read all 5 run logs directly. Every run
  did genuinely good engineering (confirmed Q2's suspicion, designed a
  real auth fix, wrote passing tests, even hand-verified against a real
  FastAPI TestClient in its sandbox) but hit a hard wall at the very end:
  `git push` and every GitHub MCP write tool returned 403 -- the cloud
  environment's GitHub connection is read-only. Reads work, writes don't.
  Not a prompt-engineering problem; revising the prompt wouldn't have
  fixed it. Needs Sam to grant the Claude GitHub App write access to
  `Vanaxity/Alfred` (github.com/apps/claude/installations/select_target
  or reconnect at claude.ai/customize/connectors) before the routine can
  ever land a PR on its own. Routine paused (`enabled: false`) until then.
- **Implemented the Q2 fix locally instead**, using the routine's design
  as a reference but building and verifying it myself -- with real
  advantages the cloud sandbox didn't have: `brain_api/auth.py`
  (stdlib-only shared secret, `ALFRED_API_KEY`), wired into HTTP
  middleware + a separate WebSocket check, `/health` left public. 10 new
  mocked tests, plus **live verification the cloud routine structurally
  couldn't do**: restarted the real server, confirmed unauthenticated
  `/api/command`/`/status` both 401, authenticated succeeds, WebSocket
  rejects without `?key=` and connects with it, and the full chain works
  through the actual live ngrok tunnel with the exact headers the
  cockpit sends.
- **The cockpit needed a matching fix or this would have broken it
  outright** -- `alfred-cockpit`'s `brainApi.ts` now sends the key on
  every request (`X-Alfred-Key` header, `?key=` for the WebSocket).
  Vercel env `NEXT_PUBLIC_ALFRED_API_KEY` set as Config (Vercel itself
  flagged the public-exposure tradeoff; accepted deliberately -- it
  blocks blind hits on a leaked ngrok URL, which was the actual Q2
  concern, not a determined attacker who's already found the cockpit and
  reads its JS). Redeployed, aliased to production, verified live.
- Also committed (from last night, previously verified but never pushed):
  the reply-truncation fix (`max_tokens` + salvage + dropped 500-char
  cap) and the cockpit's approval-gate UI. The approval flow's deeper
  backend issue (exact-signature matching breaks when the model doesn't
  regenerate identical params on retry) is still open, tracked
  separately -- the UI fix alone isn't sufficient.

## 2026-08-31 — Strix pentesting slotted in as a Phase 1 exit gate

- Sam surfaced [Strix](https://github.com/usestrix/strix) (open-source AI
  pentesting, 59k★, real/legit, Apache 2.0) for dynamic pentesting of
  Alfred. Verified: needs Docker locally, not installed on this machine.
- Sam's decision: don't install Docker or use Strix's managed cloud (would
  mean sending Alfred's code/running app to a third party) right now.
  Added to `ROADMAP.md` as a **manual, Sam-only, Phase-1-exit-gate** item
  instead — last step before Phase 1 is considered done, not something
  the cloud routine or I do autonomously.
- Q2's code-level audit (missing-auth check) is unaffected and separate —
  still in progress via the cloud routine, still happens earlier than the
  Strix gate.

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
