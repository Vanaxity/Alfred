# Alfred Progress Log

Read this first at the start of any work session (autonomous or not) —
it's the "what's actually done, what's in flight, what's blocked" record
so nobody (human or Claude) has to re-derive context from scratch. Append,
don't rewrite history — newest entries at the top.

---

## 📍 Phase 1 engineering: closed (2026-09-05). Currently in: Phase 2

Phase 1's active-catch-up mode (was here, see git history if needed) is
over — Q2 and Q8 both closed same-day via local-session work, on top of
the earlier Q3/Q6 work. **Cloud routine re-enabled 2026-09-08**
(`trig_01U7DDqtuWKAsfWa6c2fU66E`, hourly at :17) — was paused 2026-09-05
in favor of local-session live testing for Phase 2; that testing is done
(see the dated entry below), so it's back on autonomous duty, prompt
refreshed to drop stale references to already-finished work. Remaining
Phase 1 items (Strix pentest gate, Q4/Q5/Q7/Q9 business questions) are
explicitly manual/non-blocking per `ROADMAP.md` — not something a
session should pick up and start working unprompted.

**Now in Phase 2** (rescoped 2026-09-05, see `ROADMAP.md`): MCP client +
one connector this week, proactive surfacing and further connectors
pushed to Phase 3. See the dated entry below for what's actually shipped
so far.

---

## 2026-09-08 — Execution log: the self-audit loop's missing persistence layer

**Note on this run**: two other Phase 3 PRs (#15 relocated heartbeat/proactive
surfacing, #16 skill validation + SemVer) were already open against this
branch when this run started, both from earlier firings today. Checked both
before picking work to avoid duplicating either. PR #16's own body names the
exact gap this PR closes: *"there's currently no durable, structured
execution log to feed [the self-audit loop] ... Building the self-audit loop
for real means building that persistence + scheduling layer first."* This PR
builds the **persistence** half only — durable storage for what a turn did,
queryable in aggregate. The **scheduling** half (where a weekly pass would
actually run — inside `brain_api/server.py`'s lifespan vs. external/cron) is
still explicitly Sam's call, per PR #16, and this PR doesn't attempt it or
assume an answer.

- **`brain/local_db.py`**: new `execution_log` table (session_id,
  task_summary truncated to 300 chars, `timings_json`, `tools_json`,
  `turns_used`, `had_tool_error`, `created_at`) plus three methods:
  - `log_execution(...)` — one row per turn.
  - `get_recent_executions(limit=50)` — parsed rows, oldest-to-newest among
    the most recent N (same convention as the existing `get_recent_context`).
  - `get_execution_stats(limit=500)` — the actual thing a future self-audit
    pass needs without re-deriving it from raw rows itself: overall
    tool-error rate, per-phase average latency (reuses the Q8 `timings`
    keys — `llm_call_ms`, `tool_execution_ms`, etc. — for free), and
    per-tool call/failure counts.
- **`brain/v2/conversation.py`**: `Alfred.execute()` now calls
  `self.db.log_execution(...)` once per turn, right before building its
  return dict — wrapped in `if self.db is not None: try/except: pass`, so a
  logging failure (or the `db=None` shape several existing test fakes
  already use, e.g. `test_speed_audit_timing.py`) can never break a live
  turn. Deliberately fire-and-blocking (not fire-and-forget like memory
  curation) since a plain local sqlite insert is cheap enough not to need
  the same async-task treatment — confirmed via the full existing timing
  test suite still passing unchanged.

**Verified (mocked, this is a cloud session — no live server/vault/keys):**
- New `build-system/test_execution_log.py`, 11 tests: `LocalDB` methods
  against a real temp-file sqlite db (round-trip, tool-error flagging,
  task-text truncation, ordering/limit, stats aggregation on empty and
  populated logs with hand-checked expected numbers), plus `Alfred.execute()`
  wiring against a spy DB (logs session_id/task/timings/tool_results/
  turns_used correctly, defaults session_id to `"unknown"` when the caller's
  context has none) and two negative cases: an `ExplodingDB` whose
  `log_execution()` always raises still lets the turn complete normally, and
  `db=None` never gets called at all (no `AttributeError`).
- Full existing suite still green: 132/133 across all `test_*.py` files
  (excluding `test_live_realistic.py`, which needs live LLM keys and
  correctly aborts without them) — 121 pre-existing + this PR's 11 new. The
  one failure is the same pre-existing, already-documented
  `test_glob_rejects_unsafe_absolute_pattern` Linux-sandbox-vs-Windows-target
  case from every earlier entry in this log; confirmed identical before and
  after this branch's changes, and this branch never touches
  `tool_executor.py`.
- Installed this sandbox's missing runtime deps at session start
  (`python-dotenv`, `numpy`, `groq`, `openai`, `google-genai`, `croniter`,
  `mcp` — none were present), same gap earlier cloud-session entries in this
  log also hit.

**Still needs a live check from Sam or a local session:**
- Whether writing one sqlite row synchronously per turn adds any perceptible
  latency against the real vault/DB under real load — Q8's own finding was
  that the LLM call dominates turn time (~85%) and everything else measured
  was negligible, so a single local insert should be well inside that noise
  floor, but this cloud sandbox has no real turn volume to confirm it against.
- Whether `get_execution_stats()`'s shape (per-phase averages, per-tool
  failure counts) is actually the right input for whatever the self-audit
  loop's eventual LLM prompt looks like — this PR built the general-purpose
  aggregate a reasonable person would want, not a self-audit-specific one,
  since the self-audit loop itself (prompt design + the scheduling decision)
  is still unbuilt.

**Explicitly not attempted**: the self-audit loop itself (weekly cron
proposing one concrete optimization) — this PR is its prerequisite, not the
feature. Also left alone: PR #15's proactive-surfacing/heartbeat work and
PR #16's skill-validation work, both already in flight and untouched by this
diff (`brain/local_db.py`'s reminder/cron methods PR #15 added aren't present
on this branch since #15 isn't merged yet — this PR's changes are additive
and shouldn't conflict with either).

## 2026-09-08 — Live-tested all of Phase 2's MCP work, fixed 2 real bugs, resumed the cloud routine

Full offline+live pass over everything Phase 2 shipped (generic client,
filesystem proof connector, `find_mcp_server`/`install_mcp_server`),
since mocked-green was never treated as the actual bar this project uses.

- **Offline**: all 9 mocked suites re-run clean, 119/119 (later 121/121
  after this session's own additions) — no regressions from two days'
  gap since last run.
- **Live, direct API**: filesystem MCP tool call through a real approval
  gate (had to specifically target `list_allowed_directories`, since
  `list_directory` collides with Alfred's own built-in tool of the same
  name — the LLM reasonably prefers the built-in on ambiguous phrasing,
  not a bug); `find_mcp_server` against the real registry (real Slack
  hit, real "no npm match" for a docker/uvx-only package); `install_mcp_server`
  live-installing `@modelcontextprotocol/server-memory` with its tools
  immediately usable in the same running session, no restart; a
  deliberately bad command failing in ~26ms (not 20-100s).
- **Live, through the cockpit UI**: ran the cockpit's own dev server
  locally against the local brain API (sidesteps the ngrok/Vercel chain
  entirely — see below) and drove a real MCP approval through the actual
  Approve/Deny buttons in the browser. Confirmed those buttons (built
  during the Q2 auth work) render and work for an MCP-sourced tool, not
  just built-ins.
- **Two real bugs found live, not caught by any mock, both fixed** (see
  `feature/day7-heartbeat` commit `8b022d5`):
  1. The already-configured filesystem server was failing to connect at
     every startup. `CONNECT_TIMEOUT_SECONDS=20` (added for the
     install-path bad-command case) was too tight for a legitimate npx
     cold spawn, which alone takes ~20s on this machine before the MCP
     handshake even starts. Raised to 45s.
  2. A multi-turn flow (incomplete tool request → Alfred asks a
     clarifying question → user answers) sometimes produced a reply
     narrating "I need your approval before running X..." **without
     ever calling the tool** — no real `awaiting_approval` gate fired,
     leaving the user approving a request that didn't exist. Extended
     the existing untooled-completion-claim detector (previously only
     caught "has been saved"-style claims) to also catch this "about to
     act" phrasing. Confirmed via ~9 live trials post-fix: the
     nudge-and-retry recovers a real tool call every time observed, but
     note this is phrase-matching, not semantic — a future paraphrase
     could still slip past it (already happened once between the first
     and second fix pass; not a closeable-in-one-session problem).
- Unrelated finding, not chased: the two-days-old cockpit "disconnected"
  issue was a stale ngrok URL baked into the Vercel build, plus the
  `vercel env rm`/`env add` steps in `start_alfred_live.ps1` failing
  silently on a CLI version bump (fixed to use `--json` output instead of
  regex-scraping text — separate commit, `alfred-cockpit` repo /
  `Ai-terminal-stuff`'s `start_alfred_live.ps1`, not this repo).
- 23 test episodes this generated in the real Obsidian vault archived to
  `C:\Coding\_archive\alfred-mcp-live-test-20260908\` afterward.
- **Cloud routine (`trig_01U7DDqtuWKAsfWa6c2fU66E`) re-enabled**, prompt
  refreshed to drop the stale "start with Q2" and "Week 1/Week 2" framing
  now that Phase 1 and 2 are both done — it should pick up whatever's
  next on `ROADMAP.md` on its own. Fired one manual run
  (`cse_01V1gSZPLJJhHCHt4ihCHWNB`) immediately after re-enabling to
  confirm it still works end-to-end before leaving it unattended; check
  `list_runs`/`get_run_log` for that session's outcome.

## 2026-09-06 — Phase 2 stretch: find_mcp_server + live install shipped

Closed the gap the generic client left: a human still had to know an
exact npm package name and hand-edit `mcp_servers.json`. Now Alfred can
find and add a server itself from a plain description.

- `find_mcp_server` (read-only, no approval): searches the **official
  MCP registry** (`registry.modelcontextprotocol.io`) — confirmed live
  during planning that this now exists and has a real search API, so the
  original plan's npm-heuristic fallback wasn't needed. Filters to npm
  packages (the only kind `mcp_client.py` can stdio-spawn), returns up to
  3 candidates with package name and required/secret env vars flagged.
- `install_mcp_server` (approval-gated, same tier as `shell`/`run_code`):
  writes the entry to `mcp_servers.json` and connects it **live, no
  restart** — refactored `MCPClientManager.connect_all()`'s per-server
  body into a reusable `connect_one()`/`_register_mcp_tool()` pair so a
  single new server can be added without touching the startup-only path.
- New system-prompt rule: never invent a value for a required/secret env
  var `find_mcp_server` flagged — ask Sam first.
- **Two real bugs found and fixed via live testing, not caught by mocks**:
  1. A bad/typo'd command could block the event loop for 20-100+s before
     failing — traced to a Windows OS command-resolution shim, not our
     code. Fixed with a `shutil.which()` pre-check (fails in ~0.06s) plus
     a 20s `asyncio.wait_for` as defense for a server that spawns but
     hangs mid-handshake.
  2. **The live-install path silently broke every session it created.**
     Calling `connect_one()` from an HTTP request task (as opposed to the
     app lifespan's own long-lived task) crashed with `RuntimeError:
     Attempted to exit a cancel scope that isn't the current task's
     current cancel scope` once the request finished — anyio ties a
     cancel scope to the task that opened it. The install itself
     reported success, but the very next tool call on that server failed
     with "Connection closed." Fixed by routing every actual
     spawn/connect/disconnect through one persistent worker task owned by
     `MCPClientManager`, regardless of which task calls the public
     methods — verified live: install → real tool call → real result,
     twice, zero errors in the server log either time.
- **Live-verified end-to-end for real**: real registry search against the
  production API: real approval-gated install of
  `@modelcontextprotocol/server-filesystem` under a new name; a real
  follow-up tool call on the newly-installed server returned a real
  directory listing, all in the same running process. Test episodes this
  generated in the real Obsidian vault were archived out afterward — not
  genuine user conversations.
- 21 new mocked tests (`test_find_mcp_server.py` + additions to
  `test_mcp_client.py`), full suite 119/119.

## 2026-09-05 — Phase 2: generic MCP client shipped and live-verified

Sam's real ask, once we got past "which connector first": Alfred should
plug into **any** MCP server the same way — Slack, Telegram, or
literally "Nuclear music player MCP" all wire up through one client via
config, not bespoke code per service. That's exactly the manifesto's
original Phase 2 spec, not a reframing.

- `brain/mcp_client.py`: reads `mcp_servers.json` (the same config shape
  every MCP client already uses), spawns each server via the official
  `mcp` SDK, discovers tools via `list_tools()`, wraps each into Alfred's
  existing `ToolResult` shape. Registers through the same
  `ToolExecutor.register()` every built-in tool uses — no new mechanism.
- `_get_tool_descriptions()` merges in what got discovered — confirmed by
  direct read that a tool registered with `ToolExecutor` alone would
  never actually reach the LLM otherwise, since that method is what
  builds the prompt. New MCP tools default to `require_approval=True` —
  a third-party server is closer to `shell`/`run_code` in trust than a
  built-in tool.
- Shipped with the official Filesystem MCP server configured as the
  proof connector (zero new credentials).
- **Live-verified for real, not just mock-tested**: real `npx`-spawned
  server connected, discovered 14 real tools; a direct handler call
  returned a real directory listing; through the actual conversation
  loop, the LLM discovered and correctly picked the MCP tool by natural
  language, got gated for approval as designed, and executed for real
  once approved — confirmed via the server's own `thinking` trace.
- Found and fixed one real regression before it shipped: the merge broke
  `test_speed_audit_timing.py`'s bare-`Alfred`-via-`object.__new__()`
  pattern (no `_mcp_tool_schemas` attribute on a partial instance) —
  fixed via `getattr(..., {})` rather than chasing every test file that
  builds a partial instance.
- 9 new mocked tests, full suite 107/107.
- **Next real connector is Sam's call, whenever** — the whole point of a
  generic client is that adding one is now a config entry + finding its
  MCP server package, not a planned milestone requiring new code.

## 2026-09-05 — Q8 live-verified and merged (local session)

Pulled the cloud routine's PR (#14) locally instead of trusting it
mock-only, per this file's own fail-safe discipline. Full mocked suite:
98/98 (the one test the cloud sandbox flagged as pre-existing-failing,
`test_glob_rejects_unsafe_absolute_pattern`, actually passes on real
Windows — Linux-sandbox-vs-real-target difference, not a real bug).

**Real numbers, closing the routine's own "what still needs a live
check" question below**: a real turn ("What are my primary goals right
now?", 2 turns, one `memory_search` tool call) —
`total=13708ms | llm_calls=11665ms | pre_loop=1948ms
(goal_expansion=1931ms) | tool_exec=0ms | prompt_build=5ms |
memory_snippets_wait=0ms`. **The LLM call itself is ~85% of total turn
time; everything else this audit measured is negligible.** The
parallelization fix is confirmed actually overlapping (`wait=0ms`, not
just correct in shape against fakes). This reframes where any future
speed work should go: provider/prompt-size on the LLM call, not tool
execution or memory retrieval — those were never the bottleneck.
Merged into this branch.

## 2026-09-05 — Q8 speed audit: turn-latency instrumentation + one real parallelization win

- **Cloud routine's GitHub write access is back.** The previous entry below
  documented every write (`git push`, GitHub MCP write tools) 403ing from
  this cloud sandbox. Tested it directly this run (throwaway branch push +
  delete) before doing any real work: push succeeded cleanly. Something
  changed since the last entry (presumably Sam granting the write scope
  described there) — this run's branch/PR proves it end-to-end.
- **Picked up Q8 (the Week 1 speed audit)** since Q2 is now closed and the
  autonomy system is already live — next unblocked, code-only item on the
  roadmap (hardware ordering and the Strix pentest are both explicitly
  Sam-only/manual, not this routine's job).
- **Instrumented `Alfred.execute()` in `brain/v2/conversation.py`** with
  real wall-clock timing per phase, answering the roadmap's actual question
  ("where does a turn's time go") instead of guessing: goal expansion,
  skill matching, the T3 memory-snippet fetch, prompt building (summed
  across turns), LLM calls (summed), tool execution (summed), mutation
  verification (summed), compression (summed), and total. Returned as a new
  `timings` dict on the response (additive — `brain_api/server.py`'s
  `ChatResponse` schema untouched, nothing consumes it there yet) and also
  appended as a one-line human-readable `[Timing] ...` entry in `thinking`,
  so it's visible in the existing UI/logs with zero new plumbing.
- **Found and fixed one genuine parallelization win while instrumenting**:
  before the main loop, goal expansion (`goal_expander.expand`, an LLM call)
  and skill matching ran sequentially, then `_get_memory_snippets` ran
  *after* both — but the memory-snippet fetch only depends on the raw task
  text, not on either of those, so it was paying its own wall-clock time
  stacked on top for no reason. Now it starts concurrently
  (`asyncio.to_thread` + `asyncio.create_task`) and is only awaited once
  needed. `memory_snippets_wait_ms` in the new timing breakdown is the
  actual regression guard here: it stays near zero when the overlap is
  working and rises if the fetch ever becomes the new tail latency.
- **Verified against the mocked suite only** (no live server, no real LLM
  keys, no local vault — this is a cloud session, per the fail-safe rules).
  New `build-system/test_speed_audit_timing.py` (4 tests, all passing) uses
  fakes for the router/memory/skill-manager/goal-expander and: (1) asserts
  the `timings` dict has the expected keys with non-negative values, (2)
  proves the parallelization is real by injecting artificial delays into
  the two independent paths and asserting the combined pre-loop time is
  well under their sum (would fail if a future edit accidentally
  re-serializes them), (3)/(4) confirm tool-execution and multi-turn LLM
  timings accumulate correctly. Ran the full existing mocked suite too,
  after installing this sandbox's missing runtime deps (`python-dotenv`,
  `numpy`, `groq`, `openai`, `google-genai` — none were present at session
  start): everything passes except `test_glob_rejects_unsafe_absolute_pattern`
  in `test_tool_executor.py`, which I confirmed (via `git stash`) already
  fails identically on `feature/day7-heartbeat` before this branch's
  changes — pre-existing and unrelated, not something this PR touches or
  should fix under its own scope.
- **What still needs a live check from Sam or a local session**: the timing
  numbers themselves are only proven correct in shape (keys present, math
  adds up, concurrency actually overlaps) against fakes with artificial
  delays — this cloud sandbox cannot make a real LLM call or hit the real
  T3 vector index, so there's no real-world magnitude data yet (e.g.
  whether `llm_call_ms` or `tool_execution_ms` actually dominates a typical
  turn, whether the T3 hybrid search embedding step is slow enough to
  matter). That real-world read is the actual point of Q8 and can only
  come from running Alfred live and looking at the `[Timing]` lines it now
  produces — this PR gives Sam the instrument, not the diagnosis.
- **Open question for Sam**: once real numbers come back, worth deciding
  whether `timings` should also flow through to `brain_api/server.py`'s
  `ChatResponse` / the cockpit UI (e.g. a small perf readout), or stay
  server-log-only via the `thinking` line. Left as a follow-up rather than
  guessed at here since it touches the cockpit's TS side too.

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
