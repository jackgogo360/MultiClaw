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
    and list-of-dicts (- key: value\n  key2: value2).
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
                if ttype == "manual":
                    triggers.append(Trigger(type=TriggerType.MANUAL))
                else:
                    triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=keywords))
    elif isinstance(raw_triggers, str):
        triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=[raw_triggers]))

    # user-invocable: true implies keyword trigger via skill name
    if not triggers and data.get("user-invocable", False) is True:
        triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=[name]))
    elif not triggers:
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
