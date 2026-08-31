# Sam's Next Steps — written down 2026-08-27

Captured from a conversation right after using Alfred for real the first time.
In the order Sam actually wants to tackle them, not the order they came up in.

---

## 1. Fix the file structure — STARTING NOW

**The problem, as described:** too many different folders holding pieces of
"Alfred" — a `project-alfred` folder, an `alfred` folder, `alfred-cockpit`,
scattered across `C:\Coding` and `C:\Coding\Ai-terminal-stuff`, each with its
own partial copy of the file system. Already flagged once before as a known
issue (`PROJECT_TRACKER.md`, Phase 2 item) but never actually done.

**What "done" looks like:** one clear, current source of truth per component
(brain/backend, cockpit frontend), everything else either genuinely deleted
or explicitly archived with a note saying why it's not live — no more "wait,
which folder is the real one" moments like the `project-alfred` path bug
found this week.

**Status:** about to survey exactly what exists before touching anything —
no plan yet, this file exists to hold the intent while that happens.

## 2. Get literate about Alfred

Sam's own words: "I don't know much about it because I'll let AI do the
coding work." Wants a real understanding of:
- How Alfred actually works today (the five-tier memory system, the tool
  registry, the turn loop) — not the aspirational manifesto version, the
  actual running-code version.
- What's actually missing/weak right now, honestly assessed.

**Status:** not started. Comes after the file structure is fixed, so the
walkthrough is over a codebase that actually looks the way it's described.

## 3. One-month roadmap: Phase 1 → Phase 4

Get from where Alfred is today through Phase 4 of the manifesto by
September 30. Phase 5 dropped (Sam's explicit call, 2026-08-27 — it's
open-ended by the manifesto's own design, doesn't fit a month by
definition). Done out of order — before #2, at Sam's request.

**Status:** first draft written — see `ROADMAP.md`. Includes a week-by-week
plan and a design for the autonomy system (working on Alfred unattended,
with fail-safe boundaries and check-ins) — the autonomy system is designed
but **not yet wired up live**, needs a separate go-ahead. Meant to be
co-authored/argued with, not accepted as final.

## 4. Improve the manifesto

General polish pass, on top of the dated status annotations already added
this week. Folds in whatever comes out of #2 and #3.

**Status:** ongoing incrementally already (speed/reliability note added
2026-08-27 from first real usage feedback — see `ALFRED_MANIFESTO_V5.md`).

---

## Also logged (not a new item, cross-referencing the manifesto)

**Reply speed** and **one recovered task failure** from Sam's first real
session with Alfred — written into the manifesto directly under the Phase 1
scorecard (2026-08-27 entry) rather than duplicated here. Real investigation
target for whenever Q8 ("does it feel good to use") comes back up.
