"""
Skill Manager for Alfred-style autonomous skill generation.

Handles:
- Skill creation from complex tasks
- Skill self-improvement when errors occur
- Skill retrieval and matching
- Ecosystem search: falls back to skills.sh when local skills don't match
"""

import ast
import hashlib
import json
import math
import operator as op
import re as _re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from .five_tier import get_memory, T2_SKILLS_DIR

VALID_TOOLS: set = {
    "chat", "calculator", "calendar", "email", "web_search", "web_fetch",
    "shell", "read_file", "write_file", "list_directory", "glob",
    "screenshot", "gws", "open_app", "time", "remember",
    "memory_save", "memory_search", "weather", "run_code",
}

def _parse_params(raw: Any) -> dict:
    """Parse a step's params from markdown (a raw string) or an already-dict value.

    Was a dead SkillManager staticmethod with zero callers — Skill.from_markdown()
    stored params as an unparsed string instead of calling it, so any skill
    reloaded from disk had unusable step params for direct execution. Module-level
    since both Skill (a dataclass, no SkillManager access) and SkillManager need it.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        import ast
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


STOP_WORDS: set = {
    "the", "a", "an", "is", "are", "do", "my", "for", "to",
    "of", "in", "on", "at", "with", "and", "or", "but", "it",
    "that", "this", "check", "run", "get", "please", "can",
    "how", "what", "find", "use", "will", "all", "be", "by",
    "from", "has", "have", "not", "we", "you", "your", "does",
}


def _bump_patch_version(version: str) -> str:
    """SemVer patch bump for a skill improvement. Falls back to a fresh
    patch-1 version if the stored string isn't a clean X.Y.Z (e.g. a skill
    saved before versioning existed)."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "0.1.1"
    major, minor, patch = parts
    return f"{major}.{minor}.{int(patch) + 1}"


# Params a step must supply unconditionally for the handler to do anything
# useful — mirrors the required-field checks each brain/v2/tool_executor.py
# handler makes itself (e.g. handle_web_fetch rejects a missing/non-http
# `url` before it ever touches the network). Tools whose requirements
# depend on an `action` sub-param (email, calendar) are validated
# separately in _validate_email_step/_validate_calendar_step instead of
# living in this flat table.
REQUIRED_STEP_PARAMS: Dict[str, List[str]] = {
    "calculator": ["expression"],
    "web_search": ["query"],
    "web_fetch": ["url"],
    "shell": ["command"],
    "run_code": ["code"],
    "read_file": ["path"],
    "write_file": ["path", "content"],
    "open_app": ["app_name"],
    "remember": ["key", "value"],
    "memory_save": ["content"],
    "memory_search": ["query"],
}


def _missing_required_params(tool: str, params: dict) -> List[str]:
    return [p for p in REQUIRED_STEP_PARAMS.get(tool, []) if not params.get(p)]


def _validate_email_step(params: dict) -> Optional[str]:
    action = params.get("action", "triage")
    if action == "send" and not params.get("to"):
        return "email action 'send' needs 'to'"
    if action == "read" and not (params.get("query") or params.get("email_id")):
        return "email action 'read' needs 'email_id'"
    return None


def _validate_calendar_step(params: dict) -> Optional[str]:
    action = params.get("action", "agenda")
    if action == "create" and not (params.get("summary") or params.get("title")):
        return "calendar action 'create' needs 'summary'/'title'"
    if action == "delete" and not (params.get("summary") or params.get("query")):
        return "calendar action 'delete' needs 'summary'/'query'"
    return None


# Deliberately a smaller allowlist than tool_executor.handle_calculator's --
# this only needs to catch a garbage/unevaluable expression before a skill
# is trusted, not reproduce the real calculator tool. Not imported from
# tool_executor: that module lives under brain/v2, whose package __init__
# imports conversation.py, which imports SkillManager -- importing it from
# here would be a circular import at module-load time.
_CALC_SAFE_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.Pow: op.pow,
    ast.USub: op.neg, ast.UAdd: op.pos,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod,
}
_CALC_SAFE_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
}
_CALC_SAFE_NAMES = {"pi": math.pi, "e": math.e}


def _dry_run_calculator(expression: str) -> Optional[str]:
    """Safely evaluate a calculator expression for skill validation.

    Returns None if it evaluates to a real number, else an error string.
    Pure arithmetic on literals — no I/O, no side effects, so this is the
    one tool this module actually dry-runs rather than checking structurally.
    """

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _CALC_SAFE_OPS:
            return _CALC_SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_SAFE_OPS:
            return _CALC_SAFE_OPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_SAFE_FUNCS:
            return _CALC_SAFE_FUNCS[node.func.id](*[_eval(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in _CALC_SAFE_NAMES:
            return _CALC_SAFE_NAMES[node.id]
        raise ValueError(f"unsupported expression element: {type(node).__name__}")

    try:
        result = _eval(ast.parse(expression, mode="eval"))
        if not isinstance(result, (int, float)) or isinstance(result, bool):
            return "non-numeric result"
        return None
    except Exception as e:
        return str(e)


@dataclass
class SkillValidation:
    valid: bool
    reasons: List[str]


@dataclass
class Skill:
    skill_id: str
    title: str
    description: str
    steps: List[Dict[str, str]]
    tags: List[str]
    complexity: str
    success_count: int = 0
    failure_count: int = 0
    path: str = ""
    version: str = "0.1.0"

    def to_markdown(self) -> str:
        steps_md = "\n\n".join(
            [
                f"### Step {i + 1}: {step.get('description', 'Unknown')}\n"
                f"- **Tool:** `{step.get('tool')}`\n"
                f"- **Params:** `{step.get('params', '{}')}`"
                for i, step in enumerate(self.steps)
            ]
        )

        total = self.success_count + self.failure_count
        rate = f"{self.success_count}/{total}" if total else "No data"

        return f"""# Learned Skill: {self.title}

**Skill ID:** `{self.skill_id}`
**Version:** {self.version}
**Created:** {datetime.now().strftime("%Y-%m-%d %H:%M")}
**Complexity:** {self.complexity}
**Success Rate:** {rate}

---

## Description
{self.description}

## Steps
{steps_md}

## Tags
{', '.join([f'#{t}' for t in self.tags])}

---
*Auto-generated by Alfred Brain*
"""

    @classmethod
    def from_markdown(cls, path: str, content: str) -> "Skill":
        lines = content.split("\n")
        skill_id = ""
        title = ""
        description = ""
        complexity = "moderate"
        version = "0.1.0"
        tags = []
        steps = []

        for i, line in enumerate(lines):
            if "Skill ID:" in line:
                parts = line.split("`")
                if len(parts) > 1:
                    skill_id = parts[1]
            elif "Learned Skill:" in line:
                title = line.replace("# Learned Skill:", "").replace("# Learned Skill:", "").strip()
            elif line.strip() == "## Description" and i + 1 < len(lines):
                description = lines[i + 1].strip().strip('"').strip("'")
            elif "Complexity:" in line:
                parts = line.split("**Complexity:**")
                if len(parts) > 1:
                    complexity = parts[1].strip()
            elif "**Version:**" in line:
                parts = line.split("**Version:**")
                if len(parts) > 1:
                    version = parts[1].strip()

        current_step = {}
        for line in lines:
            if line.startswith("### Step") or "Step" in line:
                if current_step and "tool" in current_step:
                    steps.append(current_step)
                current_step = {}
                if ":" in line:
                    current_step["description"] = line.split(":", 1)[1].strip()
            elif "**Tool:**" in line or "Tool:" in line:
                parts = line.split("`")
                if len(parts) > 1:
                    current_step["tool"] = parts[1]
                elif ":" in line:
                    current_step["tool"] = line.split(":", 1)[1].strip()
            elif "**Params:**" in line or "Params:" in line:
                raw_params = line.split("`")[1] if "`" in line else "{}"
                current_step["params"] = _parse_params(raw_params)

        if current_step:
            steps.append(current_step)

        return cls(
            skill_id=skill_id,
            title=title,
            description=description,
            steps=steps,
            tags=tags,
            complexity=complexity,
            path=path,
            version=version,
        )


class SkillManager:
    ECOSYSTEM_THROTTLE_SECS = 60

    def __init__(self):
        self.memory = get_memory()
        self._skills_cache: Dict[str, Skill] = {}
        self._ecosystem_cache: Dict[str, tuple] = {}
        self._load_skills_metadata()

    def _load_skills_metadata(self):
        self._skills_cache = {}
        for f in sorted(T2_SKILLS_DIR.glob("*.md")):
            if f.name == "README.md":
                continue
            try:
                content = f.read_text(encoding="utf-8")
                skill = Skill.from_markdown(str(f), content)
                if self._is_low_quality_skill(skill):
                    continue
                self._skills_cache[skill.skill_id] = skill
            except Exception as e:
                print(f"Error loading skill {f}: {e}")

    def _embed_text(self, text: str) -> Optional[List[float]]:
        try:
            if self.memory._t3_vector_model is None:
                return None
            return self.memory._t3_vector_model.encode(text).tolist()
        except Exception:
            return None

    # ─── skills.sh ecosystem integration ────────────────────────────────

    SKILLS_SH_INSTALL_DIRS = [
        Path.home() / ".agents" / "skills",
        Path.home() / ".config" / "opencode" / "skills",
    ]

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return _re.compile(r'\x1b\[[0-9;]*[mK]').sub('', text)

    def _discover_skills_sh_skill_path(self, skill_name: str) -> Optional[Path]:
        for base in self.SKILLS_SH_INSTALL_DIRS:
            candidate = base / skill_name / "SKILL.md"
            if candidate.exists():
                return candidate
        return None

    def _parse_npx_find_output(self, output: str) -> List[Dict]:
        clean = self._strip_ansi(output)
        results = []
        for line in clean.split("\n"):
            line = line.strip()
            m = _re.match(r'^([\w.-]+/[\w.-]+)@([\w.-]+)', line)
            if m:
                repo = m.group(1)
                skill = m.group(2)
                installs = 0
                im = _re.search(r'([\d.]+[KMB]?)\s*installs?', line)
                if im:
                    raw = im.group(1)
                    multiplier = 1
                    if raw.endswith('K'):
                        multiplier = 1000
                        raw = raw[:-1]
                    elif raw.endswith('M'):
                        multiplier = 1000000
                        raw = raw[:-1]
                    try:
                        installs = int(float(raw.replace(',', '')) * multiplier)
                    except ValueError:
                        pass
                results.append({
                    "repo": repo,
                    "skill": skill,
                    "installs": installs,
                    "url": f"https://skills.sh/{repo}/{skill}",
                })
        return results

    def _search_skills_sh(self, query: str, max_results: int = 5) -> List[Dict]:
        now = time.time()
        cache_key = f"{query}:{max_results}"
        if cache_key in self._ecosystem_cache:
            cached_results, cached_at = self._ecosystem_cache[cache_key]
            if now - cached_at < self.ECOSYSTEM_THROTTLE_SECS:
                return cached_results
        try:
            result = subprocess.run(
                ["npx", "skills", "find", query],
                capture_output=True, text=True, timeout=30,
                env={**dict(subprocess.os.environ),
                     "NO_COLOR": "1", "FORCE_COLOR": "0"},
            )
            if result.returncode != 0:
                print(f"[SkillManager] skills find exited {result.returncode}: {result.stderr[:200]}")
                return []
            results = self._parse_npx_find_output(result.stdout)
            results.sort(key=lambda r: r["installs"], reverse=True)
            results = results[:max_results]
            self._ecosystem_cache[cache_key] = (results, now)
            return results
        except FileNotFoundError:
            print("[SkillManager] npx not found — cannot search skills.sh")
            return []
        except subprocess.TimeoutExpired:
            print("[SkillManager] skills.sh search timed out")
            return []
        except Exception as e:
            print(f"[SkillManager] skills.sh search error: {e}")
            return []

    @staticmethod
    def _parse_params(raw: Any) -> dict:
        return _parse_params(raw)

    def _is_low_quality_skill(self, skill: Skill) -> bool:
        if not skill.steps:
            return True
        if not skill.title.strip():
            return True

        text_for_test_check = f"{skill.title} {skill.description} {' '.join(skill.tags)}".lower()
        is_zero_rate = skill.success_count == 0 and skill.failure_count == 0
        if is_zero_rate and any(w in text_for_test_check for w in {"test", "testing", "test-skill", "tools-test", "installed-skill"}):
            return True

        has_non_chat_step = False
        for s in skill.steps:
            tool = s.get("tool", "")
            if tool not in VALID_TOOLS:
                return True
            if tool != "chat":
                has_non_chat_step = True

        return not has_non_chat_step

    def validate_skill(self, skill: Skill) -> SkillValidation:
        """Structural + (where safe) live dry-run check before a skill is
        trusted enough to join the live T2 pool.

        Manifesto Phase 3, "Skill Validation & Versioning": "Before saving
        a T2 skill, Alfred runs a dry-run test in a sandbox (if safe). If
        the skill fails validation, it's stored in a drafts/ folder for
        revision, not deployed." Only calculator expressions are actually
        executed here (pure, deterministic, no I/O) — every other tool is
        checked structurally (known tool, required params present for the
        requested action), since dry-running email/shell/calendar/etc.
        live would mean real side effects or network calls, not a safe
        dry run at all.
        """
        if not skill.steps:
            return SkillValidation(valid=False, reasons=["skill has no steps"])

        reasons: List[str] = []
        for i, step in enumerate(skill.steps, start=1):
            tool = step.get("tool", "")
            if tool not in VALID_TOOLS:
                reasons.append(f"step {i}: unknown tool '{tool}'")
                continue

            params = step.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                reasons.append(f"step {i}: '{tool}' params is not an object")
                continue

            if tool == "email":
                err = _validate_email_step(params)
            elif tool == "calendar":
                err = _validate_calendar_step(params)
            else:
                missing = _missing_required_params(tool, params)
                err = f"'{tool}' missing required params {missing}" if missing else None
            if err:
                reasons.append(f"step {i}: {err}")
                continue

            if tool == "calculator":
                calc_err = _dry_run_calculator(str(params.get("expression", "")))
                if calc_err:
                    reasons.append(f"step {i}: calculator dry run failed: {calc_err}")

        return SkillValidation(valid=not reasons, reasons=reasons)

    def _save_as_draft(self, skill: Skill, reasons: List[str]) -> Path:
        """Persist a skill that failed validate_skill() to a drafts/
        subfolder instead of discarding it outright, per the manifesto's
        "stored in a drafts/ folder for revision, not deployed." Not glob'd
        by _load_skills_metadata() (non-recursive `T2_SKILLS_DIR.glob("*.md")`),
        so a draft never gets matched or executed until promote_draft_skill()
        moves it into the live folder."""
        drafts_dir = T2_SKILLS_DIR / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        safe_title = _re.sub(r'[^a-zA-Z0-9 ]', '', skill.title).strip().replace(' ', '-') or "skill"
        out_path = drafts_dir / f"{safe_title}-{skill.skill_id}.md"
        reasons_md = "\n".join(f"- {r}" for r in reasons)
        content = skill.to_markdown() + f"\n## Validation Failed\n{reasons_md}\n"
        out_path.write_text(content, encoding="utf-8")
        skill.path = str(out_path)
        return out_path

    def list_draft_skills(self) -> List[Skill]:
        drafts_dir = T2_SKILLS_DIR / "drafts"
        if not drafts_dir.exists():
            return []
        drafts = []
        for f in sorted(drafts_dir.glob("*.md")):
            try:
                drafts.append(Skill.from_markdown(str(f), f.read_text(encoding="utf-8")))
            except Exception as e:
                print(f"[SkillManager] Error loading draft {f}: {e}")
        return drafts

    def promote_draft_skill(self, skill_id: str) -> bool:
        """Move a drafted skill into the live T2 pool once it's been
        revised/reviewed, adding it to the in-memory cache so find_skill()
        can match it without a full metadata reload."""
        drafts_dir = T2_SKILLS_DIR / "drafts"
        if not drafts_dir.exists():
            return False
        for f in drafts_dir.glob("*.md"):
            try:
                skill = Skill.from_markdown(str(f), f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if skill.skill_id != skill_id:
                continue
            new_path = T2_SKILLS_DIR / f.name
            f.rename(new_path)
            skill.path = str(new_path)
            self._skills_cache[skill_id] = skill
            return True
        return False

    def _convert_skill_md_to_t2(self, md_path: Path) -> Optional[Skill]:
        content = md_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        frontmatter = {}
        body_lines = []
        in_fm = False
        fm_lines = []
        for i, line in enumerate(lines):
            if i == 0 and line.strip() == "---":
                in_fm = True
                continue
            if in_fm:
                if line.strip() == "---":
                    in_fm = False
                    continue
                fm_lines.append(line)
            else:
                body_lines.append(line)
        for line in fm_lines:
            m = _re.match(r"^(\w+):\s*(.+)$", line)
            if m:
                frontmatter[m.group(1).strip()] = m.group(2).strip()
        name = frontmatter.get("name", md_path.parent.name)
        desc = frontmatter.get("description", f"Imported from skills.sh: {name}")
        body = "\n".join(body_lines).strip()

        tool_patterns = [
            _re.compile(r'(?:Tool|tool):\s*`?(\w+)`?'),
            _re.compile(r'(?:use|Use|using|Using)\s+(?:the\s+)?`?(\w+)`?\s+(?:tool|command|api)?'),
            _re.compile(r'`(\w+)`\s+(?:to|command|api)'),
        ]
        tools = set()
        for pat in tool_patterns:
            for m in pat.finditer(body):
                t = m.group(1).lower().strip()
                if t in VALID_TOOLS:
                    tools.add(t)

        if not tools:
            tools.add("chat")

        steps = []
        for tool in tools:
            steps.append({
                "tool": tool,
                "description": f"Execute step using {tool}",
                "params": {},
            })

        skill_id = hashlib.md5(
            f"{name}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]
        words = [w for w in f"{name} {desc}".lower().split()
                 if w not in STOP_WORDS and len(w) > 2]
        tags = list(set(words))[:5]

        skill = Skill(
            skill_id=skill_id,
            title=name,
            description=desc,
            steps=steps,
            tags=tags,
            complexity="moderate",
            path="",
        )

        if self._is_low_quality_skill(skill):
            print(f"[SkillManager] Rejected low-quality skill: {name}", flush=True)
            return None

        validation = self.validate_skill(skill)
        if not validation.valid:
            self._save_as_draft(skill, validation.reasons)
            print(f"[SkillManager] '{name}' failed validation, stored as draft: {validation.reasons}", flush=True)
            return None

        safe_name = _re.sub(r'[^a-zA-Z0-9 ]', '', name).strip().replace(' ', '-')[:60]
        filename = f"{safe_name}-{skill_id}.md"
        out_path = T2_SKILLS_DIR / filename
        t2_content = skill.to_markdown() + f"\n*Imported from skills.sh: {name}*\n"
        out_path.write_text(t2_content, encoding="utf-8")
        skill.path = str(out_path)
        self._skills_cache[skill.skill_id] = skill
        return skill

    def _install_from_skills_sh(self, repo: str, skill_name: str) -> Optional[Skill]:
        try:
            subprocess.run(
                ["npx", "skills", "add", repo, "-s", skill_name, "-y", "-g"],
                capture_output=True, text=True, timeout=60,
            )
            md_path = self._discover_skills_sh_skill_path(skill_name)
            if md_path:
                skill = self._convert_skill_md_to_t2(md_path)
                if skill:
                    self._skills_cache[skill.skill_id] = skill
                    return skill
            return None
        except Exception as e:
            print(f"[SkillManager] skills.sh install error: {e}")
            return None

    def find_skill(self, task_description: str, search_ecosystem: bool = True) -> Optional[Skill]:
        try:
            task_emb = self._embed_text(task_description)
            if task_emb is not None:
                skills = list(self._skills_cache.values())
                if skills:
                    title_texts = [s.title for s in skills]
                    model = self.memory._t3_vector_model
                    title_embs = model.encode(title_texts).tolist()

                    norm_task = math.sqrt(sum(x * x for x in task_emb))
                    if norm_task > 0:
                        best_idx = -1
                        best_sim = 0.0
                        for i, title_emb in enumerate(title_embs):
                            dot = sum(a * b for a, b in zip(task_emb, title_emb))
                            norm_title = math.sqrt(sum(b * b for b in title_emb))
                            if norm_title > 0:
                                sim = dot / (norm_task * norm_title)
                                if sim > best_sim:
                                    best_sim = sim
                                    best_idx = i

                        if best_sim > 0.5:
                            return skills[best_idx]
        except Exception:
            pass

        task_lower = task_description.lower()
        task_words = set(task_lower.split())

        best_match = None
        best_score = 0

        for skill in self._skills_cache.values():
            title_lower = skill.title.lower()
            title_words = set(title_lower.split())

            if not title_words:
                continue

            common = task_words & title_words
            common -= STOP_WORDS

            if not common:
                continue

            title_coverage = len(common) / max(len(title_words - STOP_WORDS), 1)
            task_coverage = len(common) / max(len(task_words - STOP_WORDS), 1)
            score = (title_coverage + task_coverage) / 2

            if score > best_score and score >= 0.6:
                best_score = score
                best_match = skill

        if not best_match and search_ecosystem:
            print(f"[SkillManager] No local match — searching skills.sh for '{task_lower[:60]}...'")
            candidates = self._search_skills_sh(task_lower)
            if candidates:
                best = candidates[0]
                print(f"[SkillManager] Found: {best['repo']}@{best['skill']} ({best['installs']} installs)")
                installed = self._install_from_skills_sh(best["repo"], best["skill"])
                if installed:
                    for skill in self._skills_cache.values():
                        title_lower = skill.title.lower()
                        title_words = set(title_lower.split())
                        if not title_words:
                            continue
                        common = task_words & title_words
                        common -= STOP_WORDS
                        if not common:
                            continue
                        title_coverage = len(common) / max(len(title_words - STOP_WORDS), 1)
                        task_coverage = len(common) / max(len(task_words - STOP_WORDS), 1)
                        score = (title_coverage + task_coverage) / 2
                        if score > best_score and score >= 0.6:
                            best_score = score
                            best_match = skill

        return best_match

    def generate_skill(
        self, task: str, steps: List[Dict], task_complexity: str, had_error: bool
    ) -> Optional[Skill]:
        try:
            skill_id = hashlib.md5(
                f"{task}{datetime.now().isoformat()}".encode()
            ).hexdigest()[:12]

            title = task[:50].strip()
            if not title:
                title = f"Task {skill_id}"

            stop_words = {"the", "a", "an", "is", "are", "do", "my", "for", "to", "of", "in", "on", "at", "with", "and", "or", "but", "it", "that", "this", "check", "run", "get", "please", "can", "how", "what", "find"}
            words = [w for w in task.lower().split() if w not in stop_words and len(w) > 2]
            tags = list(set(words))[:5]

            skill = Skill(
                skill_id=skill_id,
                title=title,
                description=f"Auto-generated skill for: {task}",
                steps=steps,
                tags=tags,
                complexity=task_complexity,
                success_count=1 if not had_error else 0,
                failure_count=1 if had_error else 0,
            )

            if self._is_low_quality_skill(skill):
                print(f"[SkillManager] Rejected low-quality auto-skill: {title}", flush=True)
                return None

            validation = self.validate_skill(skill)
            if not validation.valid:
                self._save_as_draft(skill, validation.reasons)
                print(f"[SkillManager] '{title}' failed validation, stored as draft: {validation.reasons}", flush=True)
                return None

            safe_title = _re.sub(r'[^a-zA-Z0-9 ]', '', title).strip().replace(' ', '-')
            filename = f"{safe_title}-{skill_id}.md"
            filepath = T2_SKILLS_DIR / filename
            content = skill.to_markdown()
            filepath.write_text(content, encoding="utf-8")
            skill.path = str(filepath)

            self._skills_cache[skill_id] = skill

            return skill

        except Exception as e:
            print(f"[SkillManager] Failed to generate skill: {e}")
            return None

    def improve_skill(
        self, skill_id: str, improvement_note: str, new_steps: List[Dict] = None
    ) -> bool:
        """Patch a skill's steps and persist the change to both disk and the
        in-memory cache.

        Previously wrote the improvement note to disk as a trailing log
        section but never updated `skill.steps` or `self._skills_cache` --
        the next time this same skill got matched and injected into a
        prompt (`_build_system_prompt`), it still showed the old, wrong
        steps, because the cached object was never touched. new_steps now
        actually replaces the object's steps before regenerating markdown
        from it, so disk and cache agree.
        """
        skill = self._skills_cache.get(skill_id)
        if not skill:
            return False

        if new_steps:
            skill.steps = new_steps

        skill.version = _bump_patch_version(skill.version)

        improvement_section = f"""

## Skill Improvement Log
**Updated:** {datetime.now().strftime("%Y-%m-%d %H:%M")}
**Version:** {skill.version}
**Note:** {improvement_note}
"""
        updated_content = skill.to_markdown() + improvement_section
        Path(skill.path).write_text(updated_content, encoding="utf-8")
        self._skills_cache[skill_id] = skill
        return True

    def get_all_skills(self) -> List[Skill]:
        return list(self._skills_cache.values())

    def get_skill_index(self) -> str:
        skills = self.get_all_skills()
        if not skills:
            return "No skills learned yet."

        lines = ["# Skill Index\n"]
        for skill in skills:
            lines.append(f"- **{skill.title}** (ID: `{skill.skill_id}`)")
            lines.append(f"  - Complexity: {skill.complexity}")
            lines.append(f"  - Success: {skill.success_count}/{skill.success_count + skill.failure_count}")
            lines.append("")

        return "\n".join(lines)


_skill_manager_instance: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    global _skill_manager_instance
    if _skill_manager_instance is None:
        _skill_manager_instance = SkillManager()
    return _skill_manager_instance
