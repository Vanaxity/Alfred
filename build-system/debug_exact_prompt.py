"""Trace the exact system prompt the chat tool sends to the LLM."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.memory.five_tier import get_memory

mem = get_memory()
print("=== _t4_profile contents ===")
print(f"Type: {type(mem._t4_profile)}")
print(f"Is None: {mem._t4_profile is None}")
if mem._t4_profile:
    for section, data in mem._t4_profile.items():
        print(f"  Section: {section}")
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"    {k}: {v}")
        else:
            print(f"    {data}")
else:
    print("  EMPTY!")

print("\n=== get_context_for_llm() output ===")
ctx = mem.get_context_for_llm()
print(ctx[:2000])

print("\n=== Does 'Biryani' appear? ===")
print(f"'Biryani' in context: {'Biryani' in ctx}")
print(f"'favorite_food' in context: {'favorite_food' in ctx}")
