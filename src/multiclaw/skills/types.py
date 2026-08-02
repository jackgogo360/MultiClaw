"""Core types for the skill system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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
        keywords = [self.name]
        for t in self.triggers:
            if t.type == TriggerType.KEYWORD:
                for keyword in t.keywords:
                    if keyword not in keywords:
                        keywords.append(keyword)
        return keywords

    def format_metadata(self) -> str:
        return f"- {self.name}: {self.description}"

    def format_instructions(self) -> str:
        if not self.body:
            return self.format_metadata()
        skill_dir = str(Path(self.source_path).parent) if self.source_path else ""
        path_hint = (
            f"IMPORTANT: This skill's files are located at: {skill_dir}\n"
            f"Use this path to access scripts, references, and assets. "
            f"Do not search for the skill root elsewhere.\n\n"
        ) if skill_dir else ""
        return (
            f'<skill name="{self.name}">\n'
            f'{path_hint}'
            f'{self.body}\n'
            f'</skill>'
        )

    def format_resources(self) -> str:
        parts = [self.format_instructions()]
        if self.resources:
            parts.append(f"\nAvailable resources for '{self.name}':")
            for name in sorted(self.resources.keys()):
                parts.append(f"  - {name}")
        return "\n".join(parts)
