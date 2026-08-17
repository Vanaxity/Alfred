"""Direct LLM test - reproduce exactly what the chat tool sends."""
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GROQ_API_KEY", "")

from brain.memory.five_tier import get_memory
from brain.alfred import Alfred

alfred = Alfred()

# Build the EXACT system prompt the chat tool sends
t3_ctx = None
mem = alfred.memory
from pathlib import Path

t3_results = mem.t3_find_episodes("favorite food", max_results=3)
if t3_results:
    t3_parts = []
    for r in t3_results:
        try:
            content = Path(r["path"]).read_text(encoding="utf-8")[:500]
            t3_parts.append(f"### {r['title']}\n{content}")
        except Exception:
            pass
    if t3_parts:
        t3_ctx = "\n\n".join(t3_parts)

system_prompt = f"You are Alfred, Master Sam's autonomous AI assistant.\n\n{alfred._get_bootstrap_prompt(t3_context=t3_ctx)}"

print("=== SYSTEM PROMPT ===")
print(system_prompt[:3000])
print("\n=== END SYSTEM PROMPT ===\n")

# Now test the exact LLM call
async def test():
    questions = [
        "What's my favorite food?",
        "What are my career goals?",
        "How old am I and where do I study?",
    ]
    for q in questions:
        print(f">>> {q}")
        result = await alfred._call_llm(system_prompt, q, model=alfred.CHAT_MODEL, max_tokens=300, temperature=0.5)
        print(f"    RESPONSE: {result}\n")

asyncio.run(test())
