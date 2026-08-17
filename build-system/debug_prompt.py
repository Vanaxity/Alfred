"""Quick diagnostic: is T4 data in the system prompt?"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from brain import get_alfred

alfred = get_alfred()

# Build the same prompt the chat tool would use
t3_ctx = None
system_prompt = f"You are Alfred, Master Sam's autonomous AI assistant.\n\n{alfred._get_bootstrap_prompt(t3_context=t3_ctx)}"

print("=== SYSTEM PROMPT LENGTH ===")
print(f"  {len(system_prompt)} chars")

print("\n=== T4 DATA CHECK ===")
print(f"  'Biryani' in prompt: {'Biryani' in system_prompt}")
print(f"  'favorite_food' in prompt: {'favorite_food' in system_prompt}")
print(f"  'User Profile' in prompt: {'User Profile' in system_prompt}")
print(f"  'Sam' in prompt: {'Sam' in system_prompt}")
print(f"  'age' in prompt: {'age' in system_prompt}")

# Show T4 section
if "User Profile" in system_prompt:
    idx = system_prompt.find("User Profile")
    section = system_prompt[idx:idx+500]
    print(f"\n=== USER PROFILE SECTION (chars {idx}-{idx+500}) ===")
    print(section)
else:
    print("\n=== NO USER PROFILE SECTION FOUND ===")
    # Show last 500 chars
    print("Last 500 chars of prompt:")
    print(system_prompt[-500:])
