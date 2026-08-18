"""
Prompt Builder — Hermes-inspired structured system prompt assembly.

Assembles the system prompt from identity, profile, skills, memory, tools,
and rules. Enforces a hard token budget by truncating lowest-priority sections.

Priority order (highest → lowest):
    1. Identity   — who Alfred is
    2. Rules      — behavioral constraints
    3. Profile    — T4 user data (Master Sam's facts)
    4. Tools      — available tool schemas
    5. Memory     — T3 episodic snippets
    6. Skills     — T2 learned procedures

Memory ranks above Skills, not below: the manifesto treats T3 episodic
injection as a core Phase 1 ask (Sovereignty Gap #1), while the Skills
section now carries at most one specifically-matched skill rather than an
unranked handful, so it's cheaper to lose under budget pressure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Token counting — lightweight fallback if tiktoken unavailable
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    """Estimate token count.  Tries tiktoken, falls back to ~4 chars/token."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Rough heuristic: ~1 token per 4 characters for English text
        return len(text) // 4


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolSchema:
    """Structured tool schema (Hermes-style)."""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON-Schema-like

    def to_prompt_block(self) -> str:
        """Render the tool as a prompt-friendly markdown block."""
        params_str = json.dumps(self.parameters, indent=2)
        return f"{self.name}\n{self.description} Parameters:\n\n{params_str}"


@dataclass
class AssembledPrompt:
    """Result of PromptBuilder.assemble()."""
    system: str
    tool_schemas: List[ToolSchema]
    memory_snippets: List[str]
    token_count: int
    # Section names that were candidates but didn't survive fitting under
    # budget at all (not merely truncated -- truncation leaves a visible
    # marker in `system`; a full drop leaves no trace unless reported here).
    dropped_sections: List[str] = field(default_factory=list)


@dataclass
class _Section:
    """Internal: a named prompt section with priority and token count."""
    name: str
    content: str
    priority: int  # lower number = higher priority (kept first)
    token_count: int = 0


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

# Default priority constants
PRIO_IDENTITY = 1
PRIO_RULES = 2
PRIO_PROFILE = 3
PRIO_TOOLS = 4
PRIO_MEMORY = 5
PRIO_SKILLS = 6


class PromptBuilder:
    """Assembles a structured system prompt under a token budget."""

    def __init__(self, token_budget: int = 8000) -> None:
        self.token_budget = token_budget

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(
        self,
        *,
        identity: str = "",
        profile: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        memory: Optional[List[str]] = None,
        tools: Optional[List[ToolSchema]] = None,
        rules: Optional[List[str]] = None,
        extra_sections: Optional[Dict[str, str]] = None,
        token_budget: Optional[int] = None,
    ) -> AssembledPrompt:
        """
        Build the system prompt from components.

        Returns an AssembledPrompt with the final system text, tool schemas
        (passed through for the LLM router), memory snippets, and total
        token count.
        """
        budget = token_budget or self.token_budget
        sections: List[_Section] = []

        # --- Identity ------------------------------------------------
        if identity:
            sections.append(_Section(
                name="identity",
                content=identity,
                priority=PRIO_IDENTITY,
                token_count=count_tokens(identity),
            ))

        # --- Rules ---------------------------------------------------
        if rules:
            rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
            sections.append(_Section(
                name="rules",
                content=rules_text,
                priority=PRIO_RULES,
                token_count=count_tokens(rules_text),
            ))

        # --- Profile (T4) --------------------------------------------
        if profile:
            sections.append(_Section(
                name="profile",
                content=profile,
                priority=PRIO_PROFILE,
                token_count=count_tokens(profile),
            ))

        # --- Tools ---------------------------------------------------
        if tools:
            tool_blocks = [t.to_prompt_block() for t in tools]
            tools_text = "## Available Tools\n\n" + "\n".join(tool_blocks)
            sections.append(_Section(
                name="tools",
                content=tools_text,
                priority=PRIO_TOOLS,
                token_count=count_tokens(tools_text),
            ))

        # --- Skills (T2) ---------------------------------------------
        if skills:
            skill_lines = []
            for s in skills:
                title = s.get("title", "Unnamed")
                desc = s.get("description", "")
                steps = s.get("steps", [])
                skill_lines.append(f"### {title}\n{desc}")
                if steps:
                    skill_lines.append("Steps: " + " → ".join(str(st) for st in steps))
            skills_text = "## Learned Skills\n\n" + "\n\n".join(skill_lines)
            sections.append(_Section(
                name="skills",
                content=skills_text,
                priority=PRIO_SKILLS,
                token_count=count_tokens(skills_text),
            ))

        # --- Memory (T3) ---------------------------------------------
        if memory:
            mem_text = "## Relevant Past Episodes\n\n" + "\n\n---\n\n".join(memory)
            sections.append(_Section(
                name="memory",
                content=mem_text,
                priority=PRIO_MEMORY,
                token_count=count_tokens(mem_text),
            ))

        # --- Extra sections (no priority — appended as-is) -----------
        extra_blocks: List[str] = []
        for name, content in (extra_sections or {}).items():
            extra_blocks.append(f"## {name}\n{content}")

        # --- Fit within budget ---------------------------------------
        fitted = self._fit_sections(sections, budget)
        dropped = [s.name for s in sections if s.name not in {f.name for f in fitted}]

        # --- Assemble final prompt -----------------------------------
        parts: List[str] = []
        for sec in fitted:
            parts.append(sec.content)
        for block in extra_blocks:
            parts.append(block)

        system = "\n\n".join(parts)
        total_tokens = count_tokens(system)

        return AssembledPrompt(
            system=system,
            tool_schemas=tools or [],
            memory_snippets=memory or [],
            token_count=total_tokens,
            dropped_sections=dropped,
        )

    # ------------------------------------------------------------------
    # Internal: fit sections into budget
    # ------------------------------------------------------------------

    def _fit_sections(
        self, sections: List[_Section], budget: int
    ) -> List[_Section]:
        """
        Return sections that fit within the token budget.

        Strategy:
            1. Sort by priority (lowest number = highest priority).
            2. Greedily add sections from highest to lowest priority.
            3. If a section doesn't fit, try to truncate it.
            4. If truncation isn't worthwhile (< 50 tokens), skip it.
        """
        # Sort by priority (ascending = highest priority first)
        sorted_secs = sorted(sections, key=lambda s: s.priority)

        result: List[_Section] = []
        remaining = budget

        for sec in sorted_secs:
            if sec.token_count <= remaining:
                result.append(sec)
                remaining -= sec.token_count
            else:
                # Try to truncate to fit
                if remaining > 50:
                    truncated = self._truncate_section(sec, remaining)
                    if truncated:
                        result.append(truncated)
                        remaining -= truncated.token_count

        # Restore original priority order in output
        result.sort(key=lambda s: s.priority)
        return result

    def _truncate_section(
        self, section: _Section, max_tokens: int
    ) -> Optional[_Section]:
        """
        Truncate a section's content to fit within max_tokens.
        Uses binary search for efficiency.
        """
        content = section.content
        # Rough: keep first N characters
        # 1 token ≈ 4 chars, so max_chars ≈ max_tokens * 4
        max_chars = max_tokens * 4
        if len(content) <= max_chars:
            return section

        truncated_content = content[:max_chars] + "\n\u2026[truncated]"
        new_tokens = count_tokens(truncated_content)
        if new_tokens < 50:
            return None

        return _Section(
            name=section.name,
            content=truncated_content,
            priority=section.priority,
            token_count=new_tokens,
        )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demo_tool = ToolSchema(
        name="get_time",
        description="Returns the current date and time",
        parameters={"type": "object", "properties": {"tz": {"type": "string"}}},
    )
    demo_builder = PromptBuilder(token_budget=120)
    demo_result = demo_builder.assemble(
        identity="You are Alfred, a calm British butler.",
        rules=["Never invent facts.", "Be concise."],
        profile="User: Master Sam.",
        tools=[demo_tool],
        memory=["User asked about the weather yesterday."],
    )
    print("=== Assembled system prompt ===")
    print(demo_result.system)
    print("===============================")
    print(f"token_count: {demo_result.token_count} / budget 120")
