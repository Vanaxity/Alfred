"""Quick check: what does the new _get_bootstrap_prompt produce for the T4 section?"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.alfred import Alfred

alfred = Alfred()
prompt = alfred._get_bootstrap_prompt()

print("=== FULL PROMPT ===")
print(prompt[:4000])
print("\n=== Does 'Biryani' appear? ===")
print(f"'Biryani' in prompt: {'Biryani' in prompt}")
print(f"'favorite_food' in prompt: {'favorite_food' in prompt}")
print(f"'age' in prompt: {'age' in prompt}")
print(f"'AI Research' in prompt: {'AI Research' in prompt}")
