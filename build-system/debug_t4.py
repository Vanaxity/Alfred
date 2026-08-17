"""Diagnostic script to trace T4 profile data flow."""
import sys
sys.path.insert(0, ".")

from brain.memory.five_tier import FiveTierMemory

mem = FiveTierMemory()

# What did the parser actually produce?
print("=== T4 PROFILE KEYS ===")
for section, data in mem._t4_profile.items():
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"  [{section}] key={k!r}  value={v[:60]!r}")

# Test t4_get
print("\n=== T4 GET TESTS ===")
for key in ["favorite_food", "name", "age", "education", "alias"]:
    val = mem.t4_get(key)
    print(f"  t4_get({key!r}) = {val!r}")

# What does get_context_for_llm produce?
print("\n=== CONTEXT FOR LLM (first 800 chars) ===")
ctx = mem.get_context_for_llm()
print(ctx[:800])

# Check raw Sam.md content
from pathlib import Path
sam_path = Path(r"C:\Coding\notes idk obsidian\Aflred-brain\Memory\T4-UserProfile\Sam.md")
print(f"\n=== SAM.MD EXISTS: {sam_path.exists()} ===")
if sam_path.exists():
    lines = sam_path.read_text(encoding="utf-8").split("\n")
    print(f"  Total lines: {len(lines)}")
    print("  First 10 lines:")
    for l in lines[:10]:
        print(f"    {l!r}")
