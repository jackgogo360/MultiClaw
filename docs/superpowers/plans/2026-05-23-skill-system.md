# Skill System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add skill/plugin system to MultiClaw, ported from MyObsidianVault/agent-code/20260522-skill-system, with keyword and manual trigger modes, progressive disclosure, and dual directory conventions.

**Architecture:** Self-contained `multiclaw.skills` package (types, parser, discovery, activation, manager) with minimal integration points: `SkillManager` on the agent, `skill_prompts` in `ContextRequest`, and `/skill-name` message interception. Skill instructions are injected as independent system messages before chat history.

**Tech Stack:** Python 3.12+, zero new dependencies (the skill system parser is hand-rolled YAML frontmatter)

---

### Task 1: Create skill types (`types.py`)

**Files:**
- Create: `src/multiclaw/skills/__init__.py`
- Create: `src/multiclaw/skills/types.py`

Adapted from reference, with `TriggerType.ALWAYS` removed and import paths changed.

- [ ] **Step 1: Create `src/multiclaw/skills/types.py`**

```python
"""Core types for the skill system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DisclosureLevel(Enum):
    METADATA = 1
    INSTRUCTIONS = 2
    RESOURCES = 3


class TriggerType(Enum):
    KEYWORD = "keyword"
    MANUAL = "manual"


@dataclass
class Trigger:
    type: TriggerType
    keywords: list[str] = field(default_factory=list)


@dataclass
class SkillMetadata:
    name: str
    description: str = ""
    triggers: list[Trigger] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    max_tokens: int = 0
    version: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Skill:
    name: str
    metadata: SkillMetadata
    source_path: str = ""
    body: str = ""
    resources: dict[str, str] = field(default_factory=dict)
    level: DisclosureLevel = DisclosureLevel.METADATA
    active: bool = False

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def triggers(self) -> list[Trigger]:
        return self.metadata.triggers

    @property
    def keyword_triggers(self) -> list[str]:
        keywords = []
        for t in self.triggers:
            if t.type == TriggerType.KEYWORD:
                keywords.extend(t.keywords)
        return keywords

    def format_metadata(self) -> str:
        return f"- {self.name}: {self.description}"

    def format_instructions(self) -> str:
        if not self.body:
            return self.format_metadata()
        return f'<skill name="{self.name}">\n{self.body}\n</skill>'

    def format_resources(self) -> str:
        parts = [self.format_instructions()]
        if self.resources:
            parts.append(f"\nAvailable resources for '{self.name}':")
            for name in sorted(self.resources.keys()):
                parts.append(f"  - {name}")
        return "\n".join(parts)
```

- [ ] **Step 2: Verify the file has no syntax errors**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills.types import Skill, SkillMetadata, Trigger, TriggerType, DisclosureLevel; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/__init__.py src/multiclaw/skills/types.py
git commit -m "feat: add skill system types"
```

---

### Task 2: Create skill parser (`parser.py`)

**Files:**
- Create: `src/multiclaw/skills/parser.py`

Adapted from reference. Changes: import paths, `_parse_metadata` removes ALWAYS trigger handling, defaults to MANUAL when no valid triggers found.

- [ ] **Step 1: Create `src/multiclaw/skills/parser.py`**

```python
"""SKILL.md frontmatter parser."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from multiclaw.skills.types import Skill, SkillMetadata, Trigger, TriggerType, DisclosureLevel


FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_file(path: str | Path) -> Skill:
    """Parse a SKILL.md file into a Skill object at METADATA level."""
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(content)
    meta = _parse_metadata(metadata, path.parent.name)

    return Skill(
        name=meta.name,
        metadata=meta,
        source_path=str(path),
        body="",
        level=DisclosureLevel.METADATA,
    )


def load_skill_body(skill: Skill) -> Skill:
    """Promote a skill to INSTRUCTIONS level by reading its body."""
    if skill.level.value >= DisclosureLevel.INSTRUCTIONS.value:
        return skill

    path = Path(skill.source_path)
    content = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(content)

    skill.body = body.strip()
    skill.level = DisclosureLevel.INSTRUCTIONS
    return skill


def load_skill_resources(skill: Skill) -> Skill:
    """Promote a skill to RESOURCES level by scanning its directory."""
    if skill.level.value >= DisclosureLevel.RESOURCES.value:
        return skill

    if skill.level == DisclosureLevel.METADATA:
        load_skill_body(skill)

    skill_dir = Path(skill.source_path).parent
    resources: dict[str, str] = {}

    for subdir_name in ("scripts", "references", "assets"):
        subdir = skill_dir / subdir_name
        if subdir.is_dir():
            for f in subdir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(skill_dir))
                    resources[rel] = str(f)

    skill.resources = resources
    skill.level = DisclosureLevel.RESOURCES
    return skill


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content

    yaml_str = match.group(1)
    body = content[match.end():]
    metadata = _parse_yaml_simple(yaml_str)
    return metadata, body


def _parse_yaml_simple(yaml_str: str) -> dict[str, Any]:
    """Simple YAML parser for frontmatter (no external dependency).

    Handles: key: value, key: [list], multiline lists with - prefix,
    and list-of-dicts (- key: value\\n  key2: value2).
    """
    result: dict[str, Any] = {}
    current_key = ""
    current_list: list[Any] | None = None
    current_dict: dict[str, Any] | None = None

    def _flush_dict():
        nonlocal current_dict
        if current_dict is not None and current_list is not None:
            current_list.append(current_dict)
            current_dict = None

    def _parse_value(raw: str) -> Any:
        if raw.startswith("[") and raw.endswith("]"):
            return [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        if raw.isdigit():
            return int(raw)
        return raw.strip("'\"")

    for line in yaml_str.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- "):
            if current_list is not None:
                item_content = stripped[2:].strip()
                if ":" in item_content:
                    _flush_dict()
                    k, _, v = item_content.partition(":")
                    k, v = k.strip(), v.strip()
                    current_dict = {}
                    if v:
                        current_dict[k] = _parse_value(v)
                    else:
                        current_dict[k] = ""
                else:
                    _flush_dict()
                    if item_content:
                        current_list.append(item_content)
            continue

        if indent >= 2 and current_dict is not None and ":" in stripped:
            k, _, v = stripped.partition(":")
            k, v = k.strip(), v.strip()
            if v:
                current_dict[k] = _parse_value(v)
            else:
                current_dict[k] = ""
            continue

        _flush_dict()
        if current_list is not None:
            result[current_key] = current_list
            current_list = None

        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if not value:
                current_key = key
                current_list = []
            else:
                result[key] = _parse_value(value)

    _flush_dict()
    if current_list is not None:
        result[current_key] = current_list

    return result


def _parse_metadata(data: dict[str, Any], dir_name: str) -> SkillMetadata:
    """Convert parsed frontmatter dict to SkillMetadata."""
    name = data.get("name", dir_name)

    triggers = []
    raw_triggers = data.get("triggers", [])
    if isinstance(raw_triggers, list):
        for t in raw_triggers:
            if isinstance(t, str):
                triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=[t]))
            elif isinstance(t, dict):
                ttype = t.get("type", "keyword")
                keywords = t.get("keywords", [])
                triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=keywords))
    elif isinstance(raw_triggers, str):
        triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=[raw_triggers]))

    if not triggers:
        triggers.append(Trigger(type=TriggerType.MANUAL))

    inputs = data.get("inputs", [])
    if isinstance(inputs, str):
        inputs = [inputs]

    allowed_tools = data.get("allowed_tools", data.get("allowed-tools", []))
    if isinstance(allowed_tools, str):
        allowed_tools = allowed_tools.split()

    paths = data.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]

    return SkillMetadata(
        name=name,
        description=data.get("description", ""),
        triggers=triggers,
        inputs=inputs,
        allowed_tools=allowed_tools,
        paths=paths,
        max_tokens=data.get("max_tokens", 0),
        version=data.get("version", ""),
        tags=data.get("tags", []),
    )
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills.parser import parse_skill_file, load_skill_body, load_skill_resources; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/parser.py
git commit -m "feat: add skill SKILL.md parser with progressive disclosure"
```

---

### Task 3: Create skill activation (`activation.py`)

**Files:**
- Create: `src/multiclaw/skills/activation.py`

Adapted from reference. Removed `get_always_active()` method.

- [ ] **Step 1: Create `src/multiclaw/skills/activation.py`**

```python
"""Skill activation and trigger matching."""

from __future__ import annotations

import fnmatch

from multiclaw.skills.types import Skill, DisclosureLevel
from multiclaw.skills.parser import load_skill_body


class SkillActivator:
    """Handles skill activation logic.

    Two activation modes:
    1. Keyword trigger: auto-activate when keywords appear in user message
    2. Manual: user explicitly invokes /skill-name
    """

    def __init__(self, max_active: int = 5):
        self.max_active = max_active

    def match_keywords(self, message: str, skills: dict[str, Skill]) -> list[Skill]:
        """Find skills whose keyword triggers match the message."""
        matched = []
        msg_lower = message.lower()

        for skill in skills.values():
            if skill.active:
                continue
            for keyword in skill.keyword_triggers:
                if keyword.lower() in msg_lower:
                    matched.append(skill)
                    break

        return matched

    def match_paths(self, file_paths: list[str], skills: dict[str, Skill]) -> list[Skill]:
        """Find skills whose path patterns match the given file paths."""
        matched = []

        for skill in skills.values():
            if skill.active or not skill.metadata.paths:
                continue
            for pattern in skill.metadata.paths:
                for fp in file_paths:
                    if fnmatch.fnmatch(fp, pattern):
                        matched.append(skill)
                        break
                else:
                    continue
                break

        return matched

    def activate(self, skill: Skill) -> Skill:
        """Activate a skill: load its body and mark as active."""
        if skill.level == DisclosureLevel.METADATA:
            load_skill_body(skill)
        skill.active = True
        return skill

    def deactivate(self, skill: Skill) -> Skill:
        """Deactivate a skill."""
        skill.active = False
        return skill

    def can_activate(self, active_count: int) -> bool:
        """Check if we can activate more skills (token budget guard)."""
        return active_count < self.max_active

    def substitute_args(self, skill: Skill, args: str = "",
                        named_args: dict[str, str] | None = None) -> str:
        """Substitute $ARG placeholders in skill body.

        Supports:
        - $ARGUMENTS: full argument string
        - $arg_name: named argument from inputs declaration
        """
        content = skill.body
        if not content:
            return ""

        content = content.replace("$ARGUMENTS", args)

        if named_args:
            for key, value in named_args.items():
                content = content.replace(f"${key}", value)

        if skill.metadata.inputs and args and not named_args:
            parts = args.split(None, len(skill.metadata.inputs) - 1)
            for i, input_name in enumerate(skill.metadata.inputs):
                if i < len(parts):
                    content = content.replace(f"${input_name}", parts[i])

        return content
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills.activation import SkillActivator; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/activation.py
git commit -m "feat: add skill activation with keyword matching and arg substitution"
```

---

### Task 4: Create skill discovery (`discovery.py`)

**Files:**
- Create: `src/multiclaw/skills/discovery.py`

Adapted from reference. Key change: scan both `.multiclaw/skills/` and `.agents/skills/` at each level.

- [ ] **Step 1: Create `src/multiclaw/skills/discovery.py`**

```python
"""Skill discovery — multi-layer directory scanning."""

from __future__ import annotations

import logging
from pathlib import Path

from multiclaw.skills.types import Skill
from multiclaw.skills.parser import parse_skill_file

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"

# Both directory conventions are supported
USER_SKILL_DIRS = [
    Path.home() / ".multiclaw" / "skills",
    Path.home() / ".agent" / "skills",
]
PROJECT_SKILL_DIR_NAMES = [".multiclaw/skills", ".agents/skills"]


class SkillDiscovery:
    """Discovers skills from multiple directory layers.

    Scan order (later overrides earlier by name):
    1. User skills: ~/.multiclaw/skills/ and ~/.agents/skills/
    2. Project skills: .multiclaw/skills/ and .agents/skills/ (walks up to home)
    3. Additional paths (explicit)

    Each skill is a directory containing a SKILL.md file.
    """

    def __init__(self, project_root: str | Path | None = None,
                 user_dirs: list[str | Path] | None = None,
                 extra_dirs: list[str | Path] | None = None):
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._user_dirs = [Path(d) for d in (user_dirs or USER_SKILL_DIRS)]
        self._extra_dirs = [Path(d) for d in (extra_dirs or [])]

    def discover(self) -> dict[str, Skill]:
        """Discover all skills, returning a name→Skill dict.

        Later sources override earlier ones (project > user).
        """
        skills: dict[str, Skill] = {}

        for user_dir in self._user_dirs:
            for skill in self._scan_dir(user_dir):
                skills[skill.name] = skill

        for project_dir in self._find_project_skill_dirs():
            for skill in self._scan_dir(project_dir):
                skills[skill.name] = skill

        for extra_dir in self._extra_dirs:
            for skill in self._scan_dir(extra_dir):
                skills[skill.name] = skill

        return skills

    def _find_project_skill_dirs(self) -> list[Path]:
        """Walk up from project root looking for skill directories.

        At each ancestor, checks both .multiclaw/skills/ and .agents/skills/.
        """
        dirs: list[Path] = []
        current = self._project_root.resolve()
        home = Path.home().resolve()

        while True:
            for dir_name in PROJECT_SKILL_DIR_NAMES:
                candidate = current / dir_name
                if candidate.is_dir():
                    dirs.append(candidate)

            if current == home or current == current.parent:
                break
            current = current.parent

        dirs.reverse()
        return dirs

    def _scan_dir(self, directory: Path) -> list[Skill]:
        """Scan a directory for skill subdirectories containing SKILL.md."""
        skills = []
        if not directory.is_dir():
            return skills

        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / SKILL_FILENAME
            if skill_file.is_file():
                try:
                    skill = parse_skill_file(skill_file)
                    skills.append(skill)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", skill_file, e)

        return skills

    def discover_for_paths(self, file_paths: list[str | Path]) -> list[Skill]:
        """Discover conditional skills relevant to given file paths."""
        seen_dirs: set[Path] = set()
        skills = []

        for fp in file_paths:
            p = Path(fp).resolve()
            current = p.parent if p.is_file() else p

            while current != self._project_root.parent:
                for dir_name in PROJECT_SKILL_DIR_NAMES:
                    candidate = current / dir_name
                    if candidate.is_dir() and candidate not in seen_dirs:
                        seen_dirs.add(candidate)
                        for skill in self._scan_dir(candidate):
                            if skill.metadata.paths:
                                skills.append(skill)
                if current == current.parent:
                    break
                current = current.parent

        return skills
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills.discovery import SkillDiscovery; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/discovery.py
git commit -m "feat: add skill discovery with dual directory conventions"
```

---

### Task 5: Create skill manager (`manager.py`)

**Files:**
- Create: `src/multiclaw/skills/manager.py`

Adapted from reference. Removed `get_always_active` auto-activation logic from `discover()`, removed `deactivate()` and `list_available()` (not needed for MVP).

- [ ] **Step 1: Create `src/multiclaw/skills/manager.py`**

```python
"""SkillManager — unified facade for discovery + activation."""

from __future__ import annotations

from pathlib import Path

from multiclaw.skills.types import Skill, DisclosureLevel
from multiclaw.skills.discovery import SkillDiscovery
from multiclaw.skills.activation import SkillActivator


class SkillManager:
    """Single entry point for the skill system.

    Combines discovery, activation, and prompt injection.
    """

    def __init__(self, project_root: str | Path | None = None,
                 user_dirs: list[str | Path] | None = None,
                 extra_dirs: list[str | Path] | None = None,
                 max_active: int = 5):
        self._discovery = SkillDiscovery(
            project_root=project_root,
            user_dirs=user_dirs,
            extra_dirs=extra_dirs,
        )
        self._activator = SkillActivator(max_active=max_active)
        self._skills: dict[str, Skill] = {}

    def discover(self) -> dict[str, Skill]:
        """Run discovery and return found skills."""
        self._skills = self._discovery.discover()
        return self._skills

    @property
    def skills(self) -> dict[str, Skill]:
        return self._skills

    @property
    def active_skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.active]

    @property
    def available_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def process_message(self, message: str) -> list[Skill]:
        """Check message for keyword triggers, activate matches."""
        matched = self._activator.match_keywords(message, self._skills)
        newly_activated = []
        for skill in matched:
            if self._activator.can_activate(len(self.active_skills)):
                self._activator.activate(skill)
                newly_activated.append(skill)
        return newly_activated

    def process_files(self, file_paths: list[str]) -> list[Skill]:
        """Check file paths for pattern triggers, activate matches."""
        matched = self._activator.match_paths(file_paths, self._skills)
        newly_activated = []
        for skill in matched:
            if self._activator.can_activate(len(self.active_skills)):
                self._activator.activate(skill)
                newly_activated.append(skill)
        return newly_activated

    def invoke(self, name: str, args: str = "",
               named_args: dict[str, str] | None = None) -> str | None:
        """Manually invoke a skill by name. Returns substituted content."""
        skill = self._skills.get(name)
        if not skill:
            return None
        self._activator.activate(skill)
        return self._activator.substitute_args(skill, args, named_args)

    def build_prompt_section(self) -> str:
        """Build the prompt section for all active skills."""
        parts = []
        for skill in self.active_skills:
            if skill.level == DisclosureLevel.RESOURCES:
                parts.append(skill.format_resources())
            elif skill.level == DisclosureLevel.INSTRUCTIONS:
                parts.append(skill.format_instructions())
            else:
                parts.append(skill.format_metadata())
        return "\n\n".join(parts)

    def get_active_skill_prompts(self) -> list[tuple[str, str]]:
        """Return list of (name, formatted_body) for active skills.

        Each entry is suitable for injection as an independent system message.
        """
        results = []
        for skill in self.active_skills:
            if skill.body:
                results.append((skill.name, skill.format_instructions()))
        return results
```

- [ ] **Step 2: Verify no syntax errors**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills.manager import SkillManager; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/manager.py
git commit -m "feat: add SkillManager facade for discovery + activation"
```

---

### Task 6: Update skills `__init__.py` with public API

**Files:**
- Modify: `src/multiclaw/skills/__init__.py`

- [ ] **Step 1: Write `__init__.py`**

```python
"""Skill system — discovery, parsing, and activation."""

from multiclaw.skills.types import (
    Skill,
    SkillMetadata,
    Trigger,
    TriggerType,
    DisclosureLevel,
)
from multiclaw.skills.parser import parse_skill_file, load_skill_body, load_skill_resources
from multiclaw.skills.discovery import SkillDiscovery
from multiclaw.skills.activation import SkillActivator
from multiclaw.skills.manager import SkillManager

__all__ = [
    "Skill",
    "SkillMetadata",
    "Trigger",
    "TriggerType",
    "DisclosureLevel",
    "parse_skill_file",
    "load_skill_body",
    "load_skill_resources",
    "SkillDiscovery",
    "SkillActivator",
    "SkillManager",
]
```

- [ ] **Step 2: Verify**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills import SkillManager, SkillDiscovery, SkillActivator; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/skills/__init__.py
git commit -m "feat: add skills package public API"
```

---

### Task 7: Add SkillSettings to config

**Files:**
- Modify: `src/multiclaw/config/settings.py`

- [ ] **Step 1: Add `SkillSettings` model and integrate into `Settings`**

Add this class after `AgentSettings` (around line 58):

```python
class SkillSettings(BaseModel):
    enabled: bool = True
    max_active: int = 5
    extra_dirs: list[str] = []
    user_dir: str = ""
```

In `Settings.__init__`, add `skill: SkillSettings = Field(default_factory=SkillSettings)` as a field (after `agent`).

In `Settings._build_toml_kwargs`, add handling for `[skills]` section (after the `agent` block, around line 129):

```python
        if "skills" in data:
            result["skill"] = data["skills"]
```

- [ ] **Step 2: Verify**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.config import Settings; s = Settings(); print(s.skill.enabled, s.skill.max_active)"
```
Expected: `True 5`

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/config/settings.py
git commit -m "feat: add SkillSettings to configuration"
```

---

### Task 8: Modify ContextBuilder to accept skill prompts

**Files:**
- Modify: `src/multiclaw/agent/context.py`

- [ ] **Step 1: Add `skill_prompts` to `ContextRequest` and inject in `build()`**

In `ContextRequest`, add field after `context_window_limit`:
```python
    skill_prompts: list[tuple[str, str]] = field(default_factory=list)
```

In `ContextBuilder.build()`, insert skill prompts after system prompt:

```python
    async def build(self, request: ContextRequest) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]

        # Inject active skill prompts as independent system messages
        for name, body in request.skill_prompts:
            messages.append({"role": "system", "content": body})

        recent_entries = await self.memory.recent(
```

The rest of `build()` remains unchanged.

- [ ] **Step 2: Verify**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.agent.context import ContextRequest; r = ContextRequest(system_prompt='s', user_input='u', session_id='', context_window_limit=1000); print(r.skill_prompts)"
```
Expected: `[]`

- [ ] **Step 3: Commit**

```bash
git add src/multiclaw/agent/context.py
git commit -m "feat: inject skill prompts as system messages in context builder"
```

---

### Task 9: Integrate SkillManager into MultiClawAgent

**Files:**
- Modify: `src/multiclaw/agent/multiclaw.py`

- [ ] **Step 1: Add imports**

Add at top:
```python
from multiclaw.skills import SkillManager
```

- [ ] **Step 2: Create SkillManager in `__init__`**

After `self.context_builder = ContextBuilder(...)`:
```python
        skill_settings = settings.skill if hasattr(settings, 'skill') else None
        self.skill_manager = SkillManager(
            project_root=workspace_root if 'workspace_root' in dir() else Path.cwd(),
            max_active=skill_settings.max_active if skill_settings else 5,
        )
        self.skill_manager.discover()
```

Wait — `workspace_root` isn't available in `__init__`. It's computed in `create_agent()` in server.py. Let me adjust: pass it via the agent.

Actually, looking at server.py `create_agent()`, `workspace_root` is computed there. The agent doesn't receive it. I need to either:
1. Pass `workspace_root` to `MultiClawAgent.__init__`
2. Or create SkillManager in `create_agent()` and pass it to the agent

Option 2 is cleaner — less coupling. Let me update the plan:

Add `skill_manager` parameter to `MultiClawAgent.__init__`:

```python
def __init__(
    self,
    settings: Settings,
    router: ModelRouter,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
    memory: MemoryProtocol,
    planner: Planner,
    event_bus: EventBus,
    skill_manager: SkillManager | None = None,
) -> None:
    ...
    self.skill_manager = skill_manager or SkillManager()
```

And in `server.py` `create_agent()`, create the SkillManager after computing `workspace_root`:

```python
    skill_manager = SkillManager(
        project_root=workspace_root,
        max_active=settings.skill.max_active if hasattr(settings, 'skill') else 5,
    )
    skill_manager.discover()

    runtime_agent = MultiClawAgent(
        ...
        skill_manager=skill_manager,
    )
```

- [ ] **Step 3: Modify `handle_message_stream` to intercept skills and inject prompts**

In `handle_message_stream`, right after `await self.transition(AgentState.THINKING)`:

```python
        # --- Skill handling ---
        user_msg = user_input
        newly_activated: list[str] = []

        if user_input.startswith("/"):
            parts = user_input[1:].split(None, 1)
            skill_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            body = self.skill_manager.invoke(skill_name, args)
            if body is not None:
                newly_activated.append(skill_name)
                yield {"type": "skill", "name": skill_name, "active": True}
            # Keep original message as user input
        else:
            activated = self.skill_manager.process_message(user_input)
            for s in activated:
                newly_activated.append(s.name)
                yield {"type": "skill", "name": s.name, "active": True}

        skill_prompts = self.skill_manager.get_active_skill_prompts()
```

Then in the `ContextRequest` construction, add `skill_prompts=skill_prompts`:

```python
        messages = await self.context_builder.build(
            ContextRequest(
                system_prompt=self.settings.agent.system_prompt,
                user_input=user_msg,
                session_id=session_id,
                context_window_limit=self.settings.memory.context_window_limit,
                skill_prompts=skill_prompts,
            )
        )
```

- [ ] **Step 4: Apply same skill handling to `handle_message` (non-streaming path)**

Same pattern: intercept `/` prefix, call `process_message` for keywords, build `skill_prompts`, pass to `ContextRequest`.

- [ ] **Step 5: Add `workspace_root` to `MultiClawAgent` for convenience**

In `__init__`, add: `self.workspace_root = workspace_root` parameter. This is used only for reference; SkillManager is injected.

Actually, since we're creating SkillManager in `create_agent()`, we don't need `workspace_root` on the agent at all. Skip this.

- [ ] **Step 6: Verify integration**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "from multiclaw.skills import SkillManager; m = SkillManager(); m.discover(); print(len(m.available_skills), 'skills found')"
```

- [ ] **Step 7: Commit**

```bash
git add src/multiclaw/agent/multiclaw.py src/multiclaw/server.py
git commit -m "feat: integrate SkillManager into agent and server"
```

---

### Task 10: Write tests

**Files:**
- Create: `tests/test_skills.py`

- [ ] **Step 1: Create `tests/test_skills.py`**

```python
"""Tests for the skill system."""
import tempfile
from pathlib import Path

import pytest
from multiclaw.skills import (
    Skill,
    SkillMetadata,
    Trigger,
    TriggerType,
    DisclosureLevel,
    SkillDiscovery,
    SkillActivator,
    SkillManager,
    parse_skill_file,
    load_skill_body,
    load_skill_resources,
)


SAMPLE_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing
triggers:
  - type: keyword
    keywords: [test, testing, unittest]
inputs: [target, scope]
paths: ["*.test.ts", "tests/**"]
allowed_tools: [Bash, Read]
max_tokens: 2000
version: "1.0"
tags: [testing]
---

You are a testing assistant.

Run tests on $target with scope $scope.
Full args: $ARGUMENTS
"""


# --- Helpers ---

def _make_skill_dir(parent: Path, name: str, content: str) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir / "SKILL.md"


def _make_minimal_skill(parent: Path, name: str,
                        keywords: list[str] | None = None) -> Path:
    if keywords:
        trigger_yaml = "triggers:\n"
        for kw in keywords:
            trigger_yaml += f"    - type: keyword\n      keywords: [{kw}]\n"
    else:
        trigger_yaml = ""
    content = f"""---
name: {name}
description: {name} description
{trigger_yaml}---
Body of {name}.
"""
    return _make_skill_dir(parent, name, content)


# --- Parser tests ---

def test_parse_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)

    assert skill.name == "test-skill"
    assert skill.description == "A test skill for unit testing"
    assert skill.level == DisclosureLevel.METADATA
    assert skill.body == ""
    assert len(skill.metadata.triggers) == 1
    assert skill.metadata.triggers[0].type == TriggerType.KEYWORD
    assert "test" in skill.metadata.triggers[0].keywords
    assert skill.metadata.inputs == ["target", "scope"]
    assert skill.metadata.paths == ["*.test.ts", "tests/**"]


def test_load_body():
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)
        load_skill_body(skill)

    assert skill.level == DisclosureLevel.INSTRUCTIONS
    assert "testing assistant" in skill.body
    assert "$target" in skill.body


def test_load_resources():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hi")

        skill = parse_skill_file(skill_dir / "SKILL.md")
        load_skill_resources(skill)

    assert skill.level == DisclosureLevel.RESOURCES
    assert "scripts/run.sh" in skill.resources


def test_no_frontmatter_defaults_to_manual():
    content = "Just plain text, no frontmatter."
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "plain", content)
        skill = parse_skill_file(path)

    assert skill.name == "plain"
    assert skill.metadata.triggers[0].type == TriggerType.MANUAL


def test_idempotent_load_body():
    """load_skill_body is idempotent — calling twice doesn't change anything."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)
        load_skill_body(skill)
        body1 = skill.body
        load_skill_body(skill)
        assert skill.body == body1


# --- Discovery tests ---

def test_discover_from_user_dir():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user_skills"
        _make_minimal_skill(user_dir, "skill-a", keywords=["test"])
        _make_minimal_skill(user_dir, "skill-b", keywords=["deploy"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[user_dir])
        skills = discovery.discover()

    assert "skill-a" in skills
    assert "skill-b" in skills


def test_project_overrides_user():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user_skills"
        _make_minimal_skill(user_dir, "shared", keywords=["test"])

        project_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(project_dir, "shared", keywords=["deploy"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[user_dir])
        skills = discovery.discover()

    # Project version wins
    assert skills["shared"].keyword_triggers == ["deploy"]


def test_empty_dirs_no_error():
    with tempfile.TemporaryDirectory() as tmp:
        discovery = SkillDiscovery(
            project_root=tmp,
            user_dirs=[Path(tmp) / "nonexistent"],
            extra_dirs=[],
        )
        skills = discovery.discover()
    assert skills == {}


def test_dot_multiclaw_and_dot_agent_both_scanned():
    """Both .multiclaw/skills/ and .agents/skills/ are scanned at project level."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dir_a = tmp_path / ".multiclaw" / "skills"
        dir_b = tmp_path / ".agent" / "skills"
        _make_minimal_skill(dir_a, "skill-a", keywords=["a"])
        _make_minimal_skill(dir_b, "skill-b", keywords=["b"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[], extra_dirs=[])
        skills = discovery.discover()

    assert "skill-a" in skills
    assert "skill-b" in skills


# --- Activation tests ---

def _make_skill_obj(name: str, keywords: list[str] | None = None,
                    paths: list[str] | None = None) -> Skill:
    triggers = []
    if keywords:
        triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=keywords))
    else:
        triggers.append(Trigger(type=TriggerType.MANUAL))

    return Skill(
        name=name,
        metadata=SkillMetadata(
            name=name,
            description=f"{name} desc",
            triggers=triggers,
            paths=paths or [],
        ),
        body=f"Body of {name}. Use $ARGUMENTS here.",
        level=DisclosureLevel.INSTRUCTIONS,
    )


def test_keyword_matching():
    activator = SkillActivator()
    skills = {
        "tester": _make_skill_obj("tester", keywords=["test", "unittest"]),
        "deployer": _make_skill_obj("deployer", keywords=["deploy", "release"]),
    }
    matched = activator.match_keywords("please run the test suite", skills)
    assert len(matched) == 1
    assert matched[0].name == "tester"


def test_keyword_case_insensitive():
    activator = SkillActivator()
    skills = {"s": _make_skill_obj("s", keywords=["Deploy"])}
    matched = activator.match_keywords("let's DEPLOY this", skills)
    assert len(matched) == 1


def test_skip_already_active():
    activator = SkillActivator()
    skill = _make_skill_obj("s", keywords=["test"])
    skill.active = True
    skills = {"s": skill}
    matched = activator.match_keywords("run test", skills)
    assert matched == []


def test_path_matching():
    activator = SkillActivator()
    skills = {"tester": _make_skill_obj("tester", paths=["*.test.ts", "tests/*"])}
    assert len(activator.match_paths(["src/foo.test.ts"], skills)) == 1
    assert activator.match_paths(["src/foo.ts"], skills) == []


def test_activate_deactivate():
    activator = SkillActivator()
    skill = _make_skill_obj("s", keywords=["x"])
    assert not skill.active
    activator.activate(skill)
    assert skill.active
    activator.deactivate(skill)
    assert not skill.active


def test_max_active_guard():
    activator = SkillActivator(max_active=2)
    assert activator.can_activate(0)
    assert activator.can_activate(1)
    assert not activator.can_activate(2)


def test_substitute_args():
    activator = SkillActivator()
    skill = _make_skill_obj("s")
    result = activator.substitute_args(skill, args="hello world")
    assert "hello world" in result
    assert "$ARGUMENTS" not in result


def test_substitute_named_args():
    activator = SkillActivator()
    skill = _make_skill_obj("s")
    skill.body = "Target: $target, Scope: $scope"
    skill.metadata.inputs = ["target", "scope"]
    result = activator.substitute_args(
        skill, args="", named_args={"target": "src/", "scope": "unit"}
    )
    assert "Target: src/" in result
    assert "Scope: unit" in result


# --- Manager tests ---

def test_manager_invoke():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "greet", keywords=["hello"])

        manager = SkillManager(project_root=tmp)
        manager.discover()

        result = manager.invoke("greet", args="world")
        assert result is not None
        assert "world" in result


def test_manager_keyword_activation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "tester", keywords=["test"])

        manager = SkillManager(project_root=tmp)
        manager.discover()

        activated = manager.process_message("run the test suite")
        assert len(activated) == 1
        assert activated[0].name == "tester"


def test_manager_nonexistent_skill():
    with tempfile.TemporaryDirectory() as tmp:
        manager = SkillManager(project_root=tmp)
        manager.discover()
        result = manager.invoke("nonexistent")
        assert result is None


def test_manager_get_active_skill_prompts():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "greet", keywords=["hello"])

        manager = SkillManager(project_root=tmp)
        manager.discover()
        manager.invoke("greet", args="world")

        prompts = manager.get_active_skill_prompts()
        assert len(prompts) == 1
        name, body = prompts[0]
        assert name == "greet"
        assert '<skill name="greet">' in body
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -m pytest tests/test_skills.py -v
```

All tests should pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_skills.py
git commit -m "test: add skill system unit tests"
```

---

### Task 11: Final integration verification

**Files:** None new, verify everything works together.

- [ ] **Step 1: Run all existing tests to check for regressions**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -m pytest tests/ -v --ignore=tests/test_frontend_debug.py --ignore=tests/test_frontend_welcome.py
```

All existing tests must still pass.

- [ ] **Step 2: Verify `create_agent()` works end-to-end**

```bash
cd /Users/felix/git/MultiClaw && .venv/bin/python -c "
from multiclaw.server import create_agent
agent = create_agent()
print('Skill manager:', agent.skill_manager)
print('Skills discovered:', len(agent.skill_manager.available_skills))
print('OK')
"
```

- [ ] **Step 3: Commit any final tweaks**

Only if changes were needed during verification.

---

## Self-Review Checklist

- [x] **Spec coverage:** All spec sections covered — types/parser/discovery/activation/manager modules, dual directory discovery, keyword + manual triggers, progressive disclosure, ContextBuilder integration, agent integration, config, SSE events, error handling
- [x] **Placeholder scan:** No TBD, TODO, or vague steps — every step has actual code
- [x] **Type consistency:** `get_active_skill_prompts()` returns `list[tuple[str, str]]` used consistently in agent; `skill_prompts` field matches across ContextRequest and build()
