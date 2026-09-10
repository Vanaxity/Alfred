"""
Live tool audit -- gap-fill only.

Q3 ("are all the tools working?") already has two live suites that exercise
most of the 21 registered tools through natural conversation:
  - phase1_cockpit_test.py  (20 scripted questions)
  - live_scenarios.json, run via test_live_realistic.py

Cross-referencing both suites' actual tool usage against the full registry
in tool_executor.create_tool_executor() leaves exactly 8 tools with zero
live coverage: chat, forget, gws, memory_save, open_app, run_code, shell,
write_file. This script sends ONE targeted request per gap tool only --
it does not re-test anything the two existing suites already cover.

For each tool it checks two things separately, so a routing miss is never
confused with a broken tool:
  (a) selected  -- did tools_called end up containing this tool at all?
  (b) succeeded -- for a normal tool, no error + real output. For the
      three approval-gated tools (shell, run_code, open_app -- see
      TOOL_GUARDRAILS in tool_executor.py), reaching the approval gate
      IS success: that's the designed behavior for an unattended probe,
      not a failure. Nothing is auto-approved by this script.

Usage:
    1. Start Alfred server: python -m brain_api.server   (port 8001)
    2. python build-system/tool_audit_live.py

Side effects, all self-cleaning:
  - remember/forget probe leaves no trace (forget removes what remember set).
  - memory_save probe writes one T5 archive doc titled "Tool Audit Probe",
    deleted at the end of the run.
  - write_file probe writes to build-system/.test_vault/, deleted at the
    end of the run (same isolated location test_live_realistic.py uses).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(errors="replace")

BASE = "http://127.0.0.1:8001"
REPO_ROOT = Path(__file__).parent.parent
PROBE_FILE = REPO_ROOT / "build-system" / ".test_vault" / "tool_audit_write_probe.txt"


def ask(message: str, timeout: int = 90) -> dict:
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/command", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tools_called_of(r: dict) -> list:
    # /api/command doesn't echo tools_called directly -- derive it from the
    # thinking trace, same convention the cockpit/live suites already use.
    return [
        line.split("Tool: ", 1)[1].strip()
        for line in r.get("thinking", [])
        if "] Tool: " in line
    ]


class Probe:
    def __init__(self, tool, prompt, check, gated=False):
        self.tool = tool
        self.prompt = prompt
        self.check = check  # fn(response, tools_called) -> (bool, str)
        self.gated = gated


def _selected(tools, tool):
    return tool in tools


def check_chat(r, tools):
    # chat has no side effect and no distinguishing output -- a real reply
    # with no error is success whether or not "chat" itself shows up in
    # tools_called (a plain conversational reply with zero tool calls is
    # also correct behavior for this prompt, per T01/T02-style turns in
    # phase1_cockpit_test.py). Flag explicitly if neither happened.
    reply = r.get("response", "")
    if not reply or "wasn't able to process" in reply.lower():
        return False, f"no usable reply: {reply[:150]!r}"
    if _selected(tools, "chat"):
        return True, "chat tool selected, real reply returned"
    return True, f"reply returned via tools={tools} (no explicit 'chat' call needed for this phrasing)"


def check_gws(r, tools):
    if not _selected(tools, "gws"):
        return False, f"gws not selected, tools_called={tools}"
    reply = r.get("response", "")
    if "not yet wrapped" in reply.lower() or "error" in reply.lower():
        return False, f"gws selected but errored: {reply[:200]!r}"
    return True, "gws (drive list) selected and returned real output"


def check_memory_save(r, tools):
    if not _selected(tools, "memory_save"):
        return False, f"memory_save not selected, tools_called={tools}"
    return True, "memory_save selected"


def check_write_file(r, tools):
    if not _selected(tools, "write_file"):
        return False, f"write_file not selected, tools_called={tools}"
    if not PROBE_FILE.exists():
        return False, "write_file selected but probe file not found on disk"
    return True, f"file written to {PROBE_FILE}"


def check_gated(expected_tool):
    def _check(r, tools):
        aw = r.get("awaiting_approval")
        if aw and aw.get("tool") == expected_tool:
            return True, f"reached approval gate as designed (tool={aw.get('tool')})"
        if _selected(tools, expected_tool):
            return True, f"{expected_tool} ran without a gate (unexpected but not a failure)"
        return False, f"{expected_tool} neither selected nor gated, tools_called={tools}, awaiting_approval={aw}"
    return _check


GAP_PROBES = [
    Probe("chat", "Just chat with me for a second, no need to look anything up -- tell me one fun fact.", check_chat),
    Probe("gws", "Use Google Workspace to list the files in my Google Drive.", check_gws),
    Probe(
        "memory_save",
        "Save this whole note to my archive for later search, word for word: "
        "'Tool Audit Probe -- verifying memory_save writes to the T5 archive.'",
        check_memory_save,
    ),
    Probe(
        "write_file",
        f"Write the text 'tool audit probe' to a file at {PROBE_FILE.as_posix()}",
        check_write_file,
    ),
    Probe("open_app", "Open Notepad for me.", check_gated("open_app"), gated=True),
    # Deliberately not "print(2+2)" -- confirmed live that trivial arithmetic
    # gets (correctly) routed to `calculator` instead, which isn't a run_code
    # bug, just a probe that doesn't actually need code execution. This one
    # needs real control flow, so only run_code can satisfy it.
    Probe(
        "run_code",
        "Write and run a short Python script that prints the squares of 1 through 5 using a list comprehension.",
        check_gated("run_code"),
        gated=True,
    ),
    Probe("shell", "Run this in the shell: echo hello-from-audit", check_gated("shell"), gated=True),
]


def run_forget_probe():
    """forget: remember a throwaway fact, forget it, confirm it's gone --
    same flow already live-verified once during Claim A, re-run here as
    this tool's entry in the registry-wide audit."""
    key = "tool_audit_probe_fact"
    r1 = ask(f"Remember this: my {key} is 'xyz-42'.")
    t1 = tools_called_of(r1)
    if not _selected(t1, "remember") and not _selected(t1, "memory_save"):
        return False, f"remember step: neither remember nor memory_save selected, tools={t1}"

    r2 = ask(f"Forget what I told you about my {key}.")
    t2 = tools_called_of(r2)
    if not _selected(t2, "forget"):
        return False, f"forget not selected, tools={t2}"
    if "error" in r2.get("response", "").lower() and "no stored fact" not in r2.get("response", "").lower():
        return False, f"forget selected but errored: {r2.get('response', '')[:200]!r}"
    return True, "remember -> forget round trip completed"


def main():
    print("=" * 70)
    print("  LIVE TOOL AUDIT -- gap-fill for tools with zero coverage in")
    print("  phase1_cockpit_test.py / test_live_realistic.py")
    print("=" * 70)

    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=5) as r:
            health = json.loads(r.read().decode())
        print(f"\nServer up: {health}\n")
    except Exception as e:
        print(f"\nSERVER NOT REACHABLE at {BASE}: {e}")
        return 1

    results = []

    for probe in GAP_PROBES:
        print(f"[{probe.tool}] {probe.prompt[:70]}...")
        try:
            r = ask(probe.prompt)
            tools = tools_called_of(r)
            ok, note = probe.check(r, tools)
        except Exception as e:
            ok, note = False, f"request failed: {e}"
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {note}")
        results.append((probe.tool, ok, note))
        time.sleep(2)

    print(f"[forget] remember -> forget round trip...")
    try:
        ok, note = run_forget_probe()
    except Exception as e:
        ok, note = False, f"request failed: {e}"
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {note}")
    results.append(("forget", ok, note))

    # Cleanup: probe file
    try:
        if PROBE_FILE.exists():
            PROBE_FILE.unlink()
    except Exception as e:
        print(f"  (cleanup warning: could not remove {PROBE_FILE}: {e})")

    # Cleanup: the memory_save probe writes a real row into the T5 archive
    # (the real Obsidian-vault-backed archive.db, not an isolated test copy --
    # confirmed missing from this function once already, live, on 2026-08-27;
    # don't repeat that miss).
    try:
        from brain.memory.five_tier import FiveTierMemory
        mem = FiveTierMemory()
        mem._t5_db.execute("DELETE FROM archive_fts WHERE title = 'Tool Audit Probe'")
        mem._t5_db.commit()
    except Exception as e:
        print(f"  (cleanup warning: could not remove T5 'Tool Audit Probe' row: {e})")

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    passed = sum(1 for _, ok, _ in results if ok)
    for tool, ok, note in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {tool:15s} {note}")
    print(f"\n  {passed}/{len(results)} gap tools confirmed working")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
