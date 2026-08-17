"""
T4 User Profile Seeder
Reads NEURAL_SEED.json + GEMINI.md and writes Sam.md to the Obsidian vault.
Run once to populate the user profile with 15+ facts.

Usage:
    python -m build-system.seed_t4
    python build-system/seed_t4.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime


def find_obsidian_vault() -> Path:
    """Find the Obsidian vault path from env or default location."""
    vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    if vault:
        return Path(vault)
    
    # Default locations
    candidates = [
        Path(r"C:\Coding\notes idk obsidian\Aflred-brain"),
        Path.home() / "Documents" / "Aflred-brain",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Cannot find Obsidian vault. Set OBSIDIAN_VAULT_PATH env var."
    )


def load_neural_seed(seed_path: Path) -> dict:
    """Load NEURAL_SEED.json."""
    with open(seed_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_sam_md(seed: dict) -> str:
    """Build Sam.md content from NEURAL_SEED.json."""
    profile = seed.get("profile", {})
    family = seed.get("family", [])
    interests = seed.get("interests", [])
    short_goals = seed.get("short_term_goals", [])
    long_goals = seed.get("long_term_goals", [])
    career = seed.get("career_aspirations", {})
    notes = seed.get("alfred_notes", {})

    lines = [
        "# Sam's User Profile",
        f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "## Identity",
        f"- name: {profile.get('name', 'Sam')}",
        f"- alias: {profile.get('alias', 'Vanaxity')}",
        f"- age: {profile.get('age', 'High school student')}",
        f"- location: {profile.get('location', 'India')}",
        f"- education: {profile.get('education', 'High School')}",
        f"- role: {profile.get('role', 'Creator of Alfred')}",
        "",
        "## Family",
    ]
    for person in family:
        lines.append(f"- {person.get('relationship', 'family')}: {person.get('name', 'Unknown')}")
    lines.append("")

    lines.append("## Interests & Hobbies")
    for interest in interests:
        lines.append(f"- {interest}")
    lines.append("")

    lines.append("## Goals")
    lines.append("### Short-term (6-12 months)")
    for goal in short_goals:
        lines.append(f"- {goal}")
    lines.append("")
    lines.append("### Long-term")
    for goal in long_goals:
        lines.append(f"- {goal}")
    lines.append("")

    lines.append("## Career Aspirations")
    lines.append(f"- primary: {career.get('primary', 'AI Research')}")
    lines.append(f"- secondary: {career.get('secondary', 'Startup Founder')}")
    fields = career.get("fields", [])
    if fields:
        lines.append(f"- fields: {', '.join(fields)}")
    schools = career.get("target_schools", [])
    if schools:
        lines.append(f"- target_schools: {', '.join(schools)}")
    lines.append("")

    lines.append("## Preferences")
    lines.append(f"- favorite_food: {seed.get('favorite_food', 'Biryani')}")
    lines.append(f"- communication_style: {notes.get('communication_style', 'Direct, concise')}")
    lines.append(f"- known_since: {notes.get('known_since', datetime.now().strftime('%Y-%m-%d'))}")
    lines.append("")

    lines.append("## t4")
    lines.append(f"- study plan: Math, Coding, Finance, AI/ML daily routine")
    lines.append(f"- project: {notes.get('project', 'Alfred')}")
    lines.append(f"- learning_style: Project-based, video explanations, visual examples")
    lines.append("")

    lines.append("## goals")
    lines.append(f"- daily_learning: 2 hours")
    lines.append(f"- coding: 30-60 mins daily")
    lines.append(f"- math: AoPS / Olympiad / Problem Solving every day")
    lines.append(f"- finance: Plain Bagel + Khan Academy")
    lines.append(f"- study_structure: Monday = school only, weekends = deep work (6hr+)")

    return "\n".join(lines)


def main():
    # Find paths
    vault = find_obsidian_vault()
    t4_dir = vault / "Memory" / "T4-UserProfile"
    t4_dir.mkdir(parents=True, exist_ok=True)
    
    seed_path = Path(__file__).parent.parent / "data" / "NEURAL_SEED.json"
    if not seed_path.exists():
        print(f"ERROR: NEURAL_SEED.json not found at {seed_path}")
        sys.exit(1)
    
    # Load seed data
    seed = load_neural_seed(seed_path)
    print(f"Loaded NEURAL_SEED.json ({len(json.dumps(seed))} bytes)")
    
    # Build Sam.md
    content = build_sam_md(seed)
    
    # Write to Obsidian vault
    output_path = t4_dir / "Sam.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    # Count facts
    fact_count = content.count("- ") + content.count("## ")
    print(f"Written to: {output_path}")
    print(f"Estimated facts: {fact_count}")
    
    # Verify
    lines = [l for l in content.split("\n") if l.strip().startswith("- ")]
    print(f"Actual bullet points: {len(lines)}")
    
    if len(lines) >= 15:
        print("SUCCESS: 15+ facts written to Sam.md")
    else:
        print(f"WARNING: Only {len(lines)} facts found. Target is 15+.")
    
    print("\n--- Preview (first 20 lines) ---")
    for line in content.split("\n")[:20]:
        print(line)


if __name__ == "__main__":
    main()
